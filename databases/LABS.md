# Databases — Labs

A learn-by-doing task sheet for every chapter in `databases/`. There is no theory here: the chapter
is the theory, and each lab makes you retrieve it, build it, break it and measure it. Every task
ends in a number, an output or a file you can check.

- **Closed book first.** Answer the checkpoint questions before you reread the chapter.
- **Predict before you run.** Write the prediction down, then measure. A wrong prediction you wrote down teaches more than a right one you didn't.
- **Write results down.** A task is done when the number is in your notebook, not when the command ran.
- **Space it.** Redo the checkpoints later from memory (schedule below).

## How to use this sheet
- Order: follow the linear reading order in [MENTAL_MODEL.md](MENTAL_MODEL.md) §8 (00, 01, 02, 03, 06, 14, 05, 17, 18, 04, 15, 13, 07–10, 11, 12, 16, 19, 20, 21, 22). Do all **Core** tasks of a chapter before its **Stretch** tasks.
- Tick the `- [ ]` boxes as you finish tasks.
- Keep a `lab-notebook.md` with one row per task: `task | prediction | result | why they differ`.
- Spacing: redo each chapter's checkpoint questions closed-book 1 day, 1 week and 1 month after you finish the chapter. Reread only the sections you missed.
- The chapters' own code is referenced, not repeated: [simpledb.py](simpledb.py) (DiskManager, SlottedPage, BufferPool, WALManager, BPlusTree, LSMTree, Volcano operators) and the `failure_detection_*.py` scripts.

## Setup

| Environment | Cost | Used by |
|---|---|---|
| Laptop + Docker, Postgres 17 (`pgvector/pgvector:pg17`: official image + pgvector) with `pg_stat_statements`, `pageinspect`, `pg_buffercache`, `amcheck`, `pgstattuple`, `pg_visibility`, `pg_walinspect` | Free | 01–07, 09, 11b, 12, 14, 15, 17–19, capstones |
| Streaming replica of the same image (compose profile `repl`) | Free | 09, 12 |
| MySQL 8.4 LTS (profile `mysql`) | Free | 07 (InnoDB only) |
| Redis 7.4 (profile `redis`) | Free | 10 |
| etcd v3.5, 3 containers (commands in chapter 16 below) | Free | 16 |
| Python 3.12 venv: `pip install "psycopg[binary]" duckdb chdb pyarrow polars numpy faiss-cpu rocksdict deltalake "pyiceberg[sql-sqlite,pyarrow]" redis` | Free | most chapters |
| Go 1.22+ | Free | 00, 17, 20 |
| Linux CLI tools: `sqlite3 fio strace time` (in a Linux VM or `docker run --rm -it --privileged -v labio:/io ubuntu:24.04` on macOS) | Free | 00, 07, 17 |
| Optional: TiDB via `tiup playground`, CockroachDB `cockroach demo` | Free, heavy | 09, 19 Stretch only |

Shared Postgres stack. Create a `labs/` folder with these three files, then run `docker compose up -d`:

```yaml
# labs/docker-compose.yml
services:
  pg:
    image: pgvector/pgvector:pg17
    environment: { POSTGRES_PASSWORD: lab, POSTGRES_DB: lab, POSTGRES_INITDB_ARGS: --data-checksums }
    command: >
      postgres -c shared_preload_libraries=pg_stat_statements -c track_io_timing=on
               -c wal_level=logical -c max_prepared_transactions=10
               -c log_lock_waits=on -c log_temp_files=0 -c log_checkpoints=on
    ports: ["5432:5432"]
    shm_size: 1g
    volumes: [pgdata:/var/lib/postgresql/data, ./init:/docker-entrypoint-initdb.d:ro, ./bench:/bench:ro]
    healthcheck: { test: ["CMD", "pg_isready", "-U", "postgres"], interval: 2s, retries: 30 }
  pg-replica:
    image: pgvector/pgvector:pg17
    profiles: [repl]
    depends_on: { pg: { condition: service_healthy } }
    user: postgres
    environment: { PGPASSWORD: lab }
    command: >
      bash -c "rm -rf /tmp/rep && pg_basebackup -h pg -U postgres -D /tmp/rep -R -X stream -c fast
               && exec postgres -D /tmp/rep -c max_prepared_transactions=10"
    ports: ["5433:5432"]
  mysql:
    image: mysql:8.4
    profiles: [mysql]
    environment: { MYSQL_ROOT_PASSWORD: lab, MYSQL_DATABASE: lab }
    ports: ["3306:3306"]
  redis:
    image: redis:7.4
    profiles: [redis]
    command: redis-server --save "" --appendonly no --enable-debug-command yes
    ports: ["6379:6379"]
volumes: { pgdata: {} }
```

```bash
# labs/init/00-lab.sh   (runs once, on first start)
echo "host replication all all scram-sha-256" >> "$PGDATA/pg_hba.conf"
psql -v ON_ERROR_STOP=1 -U postgres -d lab <<'SQL'
CREATE EXTENSION pg_stat_statements; CREATE EXTENSION pageinspect; CREATE EXTENSION pg_buffercache;
CREATE EXTENSION amcheck; CREATE EXTENSION pgstattuple; CREATE EXTENSION pg_visibility;
CREATE EXTENSION pg_walinspect; CREATE EXTENSION pg_trgm; CREATE EXTENSION vector;
SQL
```

```bash
mkdir -p labs/bench                                  # custom pgbench scripts go here
alias lab='docker compose exec pg psql -U postgres lab'           # session; open two terminals for concurrency labs
alias pgb='docker compose exec pg pgbench -U postgres'            # pgb -i -s 50 lab
export PGURL=postgresql://postgres:lab@localhost:5432/lab          # for psycopg / DuckDB
```

Stats views are flushed lazily. If a counter in `pg_stat_user_tables` or `pg_stat_wal` looks stale, run `SELECT pg_stat_force_next_flush();` or read it from a second session.

---

## 00 — OS and Hardware Internals  ([chapter](00-os-and-hardware-internals.md))
**Time:** ~4 h · **Needs:** Linux (VM or privileged container), Go, fio, sqlite3, strace, GNU time

- [ ] **00.1 Draw your own latency ladder** *(Level: Core)*
  - **Goal:** See the L1/L2/L3/DRAM steps from §2 on your own machine.
  - **Do:** In Go, build a pointer-chasing array: `next []int64` of N elements forming one random cycle (Sattolo's shuffle), then time `for i := 0; i < hops; i++ { p = next[p] }`. Sweep the working set from 16 KiB to 1 GiB (×2 each step). Print ns per hop. Get cache sizes from `lscpu | grep -i cache`.
  - **Predict:** ns/hop at 32 KiB, 1 MiB, 16 MiB, 512 MiB.
  - **Verify:** A plot with plateaus whose edges line up with your cache sizes and a DRAM plateau of roughly 80–120 ns.
- [ ] **00.2 False sharing** *(Level: Core)*
  - **Goal:** Measure the cost of two cores writing the same cache line (§2, False Sharing).
  - **Do:** Two goroutines each run `atomic.AddInt64` 100M times, one on field `a`, one on `b`. Version 1: `struct{ a, b int64 }`. Version 2: `struct{ a int64; _ [56]byte; b int64 }`. Use `go test -bench`.
  - **Predict:** The slowdown factor of version 1.
  - **Verify:** Both timings in your notebook, and the ratio.
- [ ] **00.3 Buffered, direct and async reads** *(Level: Core)*
  - **Goal:** Put numbers on §8, §9 and §12.
  - **Do:** On a 4 GiB file in a Docker volume (O_DIRECT may be rejected on macOS bind mounts): `fio --name=r --filename=/io/f --size=4G --rw=randread --bs=4k --runtime=20 --time_based --ioengine=psync --direct=1`. Then run with `--direct=0` twice (cold, then warm), then `--direct=1 --ioengine=io_uring --iodepth=32`.
  - **Predict:** IOPS for each of the four runs.
  - **Verify:** A 4-row table of IOPS and p99 latency (`clat percentiles`). Explain why the warm buffered run is not a disk benchmark.
- [ ] **00.4 What fsync costs, and who calls it** *(Level: Core)*
  - **Goal:** Tie §13 to a number and to real syscall counts.
  - **Do:** `docker compose exec pg pg_test_fsync -s 3 -f /var/lib/postgresql/data/fsync.tmp`. Then generate 1,000 `INSERT` statements and run them in SQLite twice, once autocommit and once wrapped in `BEGIN; ... COMMIT;`, each under `strace -f -c -e trace=fsync,fdatasync sqlite3 t.db < ins.sql`.
  - **Predict:** fdatasync ops/s on your disk, and the sync-call count for each SQLite run.
  - **Verify:** About 1,000+ syncs versus a handful. Compute the maximum commit rate one client can reach without group commit.
- [ ] **00.5 Page faults and fault-around** *(Level: Stretch)*
  - **Goal:** Watch §5 and §10 happen.
  - **Do:** Python: `mmap` a 2 GiB file read-only and touch one byte every 4096 bytes. Run under `/usr/bin/time -v` after `sync; echo 3 > /proc/sys/vm/drop_caches` (cold), then again (warm). Repeat with `flags=mmap.MAP_SHARED | mmap.MAP_POPULATE`.
  - **Predict:** Major and minor fault counts for the cold run (2 GiB / 4 KiB = 524,288 pages).
  - **Verify:** The counts from `time -v`. If minor faults are about 16x lower than the page count, find the kernel's fault-around setting (`/sys/kernel/mm/transparent_hugepage/`, `fault_around_bytes` in debugfs) and explain it.

**Checkpoint (closed book):**
1. `write()` returned success. Name two layers that can still lose the data on power loss.
2. Give three reasons from §11 why a database should not use mmap as its buffer pool.
3. Why does a TLB shootdown get more expensive as core count grows?
<details><summary>Answers</summary>

1. The OS page cache (until fsync/fdatasync) and the drive's volatile write cache (until a flush/FUA; consumer SSDs may lie).
2. Any three of: no control over eviction; I/O stalls hidden inside page faults; SIGBUS instead of error codes; TLB shootdown storms; no control over write-back ordering (breaks WAL-before-data); no async I/O.
3. Unmapping a page requires an IPI to every core that might cache the mapping, and the initiator waits for all of them to acknowledge.
</details>

---

## 01 — Storage Engine Fundamentals  ([chapter](01-storage-engine-fundamentals.md))
**Time:** ~3 h · **Needs:** Postgres, Python

- [ ] **01.1 Read a slotted page** *(Level: Core)*
  - **Goal:** Map §3 (slotted page, line pointers, tuple header) onto real bytes.
  - **Do:** `CREATE TABLE t (id int PRIMARY KEY, v int, pad text); INSERT INTO t SELECT g, g, repeat('x',50) FROM generate_series(1,1000) g;` Then `SELECT lower, upper, special FROM page_header(get_raw_page('t',0));` and `SELECT lp, lp_off, lp_len, t_xmin, t_xmax, t_ctid, t_hoff FROM heap_page_items(get_raw_page('t',0));`
  - **Predict:** Tuples per 8 KiB page (tuple header 24 B + data, MAXALIGN 8, plus a 4 B line pointer each).
  - **Verify:** `SELECT count(*) FROM heap_page_items(get_raw_page('t',0));` matches within 1. Explain `upper - lower` as free space.
- [ ] **01.2 TIDs move, line pointers stay** *(Level: Core)*
  - **Goal:** See why indirection via line pointers exists (§3).
  - **Do:** `SELECT ctid FROM t WHERE id=5;` then `UPDATE t SET v=0 WHERE id=5;` and look again. `DELETE FROM t WHERE id BETWEEN 10 AND 20; VACUUM t;` then inspect `lp_flags` for those slots and insert new rows.
  - **Predict:** The new ctid of id 5, and which `lp` numbers the new rows reuse.
  - **Verify:** `lp_flags` values before and after VACUUM (0 unused, 1 normal, 2 redirect, 3 dead), and reused slot numbers.
- [ ] **01.3 Watch the clock sweep** *(Level: Core)*
  - **Goal:** Observe PostgreSQL's clock replacement (§6) through usage counts.
  - **Do:** Restart Postgres, run `SELECT usagecount, count(*) FROM pg_buffercache GROUP BY 1 ORDER BY 1;`, then run one point query 1,000 times (`pgb -f` with a one-line script, `-t 1000`) and query again, filtering on `relfilenode = pg_relation_filenode('t_pkey')`.
  - **Predict:** The usagecount of the index root and leaf buffers afterwards.
  - **Verify:** Hot pages sit at 5 (the cap). Compute the database hit ratio from `pg_stat_database` (`blks_hit / (blks_hit + blks_read)`).
- [ ] **01.4 Break it: corrupt a page and let the checksum catch it** *(Level: Core)*
  - **Goal:** See §10 checksums detect silent corruption.
  - **Do:** `SELECT pg_relation_filepath('t');` then `CHECKPOINT;` and `docker compose stop pg`. Flip one byte with `docker compose run --rm --user postgres pg bash -c "printf '\xff' | dd of=/var/lib/postgresql/data/<path> bs=1 seek=7000 conv=notrunc"`. Run `pg_checksums --check -D /var/lib/postgresql/data` the same way. Start pg and `SELECT count(*) FROM t;` then `SELECT * FROM verify_heapam('t');`
  - **Predict:** Which of the three detects the damage, and whether `SELECT` fails or warns.
  - **Verify:** The error text (`page verification failed, calculated checksum ... but expected ...`). Then try `SET ignore_checksum_failure = on;` and write one sentence on why that is dangerous.
- [ ] **01.5 Sequential flooding** *(Level: Stretch)*
  - **Goal:** Show why plain LRU fails under scans (§6).
  - **Do:** Write a buffer-pool simulator (or extend `BufferPool` in [simpledb.py](simpledb.py)) with LRU, Clock and LRU-2. Trace: Zipf(1.1) point reads over 100k pages, with a full sequential scan of 50k pages injected every 200k requests. Pool size 5k pages.
  - **Predict:** Hit ratio of each policy.
  - **Verify:** A table of hit ratios. LRU should drop sharply after each scan and LRU-2 should barely move.

**Checkpoint (closed book):**
1. Why do heap pages use a line-pointer array instead of storing tuple offsets in indexes?
2. What is the difference between pinning a buffer and latching it?
3. What problem do full-page writes solve, and when does Postgres emit one?
<details><summary>Answers</summary>

1. Tuples can move within a page (compaction, HOT pruning) without changing their TID; indexes point to (page, slot), and only the slot's offset changes.
2. A pin says "don't evict this frame" and can be held across operations; a latch is a short read/write lock protecting the page's bytes during one access.
3. Torn pages (a partial 8 KB write on crash). The first modification of a page after each checkpoint logs the whole page image so redo can start from a known-good copy.
</details>

---

## 02 — Data Storage Formats and Encoding  ([chapter](02-data-storage-formats-and-encoding.md))
**Time:** ~3 h · **Needs:** Postgres, Python + pyarrow

- [ ] **02.1 Type sizes** *(Level: Core)*
  - **Goal:** Retrieve §2's type representations from memory.
  - **Do:** Write down your predictions, then run `SELECT pg_column_size(1::int2), pg_column_size(1::int8), pg_column_size(1.5::numeric), pg_column_size('a'::text), pg_column_size(repeat('a',200)), pg_column_size(now()), pg_column_size(gen_random_uuid()), pg_column_size('{"a":1}'::jsonb);`
  - **Predict:** Each size in bytes, including varlena headers (1 B short header vs 4 B).
  - **Verify:** A table of predicted vs actual. Explain each miss.
- [ ] **02.2 Column tetris** *(Level: Core)*
  - **Goal:** Measure alignment padding (§3).
  - **Do:** `CREATE TABLE t1 (a bool, b int8, c bool, d int8, e bool, f int8);` and `t2` with the same columns ordered `b, d, f, a, c, e`. Insert 1M rows into each. Compare `pg_relation_size` and `lp_len` from `heap_page_items`.
  - **Predict:** `lp_len` of each (24 B header + data) and the size ratio.
  - **Verify:** Expect 72 vs 51 bytes per tuple and about 73 MB vs 57 MB.
- [ ] **02.3 The NULL bitmap edge** *(Level: Core)*
  - **Goal:** See §4's bitmap interact with header alignment.
  - **Do:** Create `n8` with 8 int columns and `n9` with 9. Insert one row without NULLs and one with a NULL into each. Read `t_hoff, lp_len, t_bits` from `heap_page_items`.
  - **Predict:** `t_hoff` for each of the four rows.
  - **Verify:** 24, 24, 24, 32. Explain why 8 columns fit a bitmap into the header's padding and 9 do not.
- [ ] **02.4 TOAST threshold and compression** *(Level: Core)*
  - **Goal:** Find where values leave the main heap (§5) and what compression buys (§6).
  - **Do:** `CREATE TABLE doc (id int, body text);` Insert bodies of 1 KB, 3 KB and 100 KB, both repetitive (`repeat('ab', n)`) and incompressible (`string_agg(md5(random()::text), '')`). Query `pg_column_size(body)`, `octet_length(body)`, `pg_column_compression(body)` and `pg_relation_size(reltoastrelid)` from `pg_class`. Then `ALTER TABLE doc ALTER COLUMN body SET COMPRESSION lz4;` and insert again.
  - **Predict:** Which rows get compressed, which are moved out of line, and whether lz4 beats pglz on size.
  - **Verify:** A table per row: stored size, compression method, and TOAST table growth.
- [ ] **02.5 Columnar encodings with Parquet** *(Level: Stretch)*
  - **Goal:** Measure the §8 encodings on real files.
  - **Do:** With pyarrow, build 10M rows: `status` (5 distinct strings), `ts` (monotonic int64), `price` (random float64). Write with `use_dictionary` on/off and `compression` none/snappy/zstd, and once sorted by `status`. Inspect `pq.ParquetFile(p).metadata.row_group(0).column(i)` for `encodings`, `total_compressed_size` and `statistics`.
  - **Predict:** Which column shrinks most and which barely compresses.
  - **Verify:** A size matrix (column × setting). `status` should shrink by orders of magnitude, `price` should barely shrink.

**Checkpoint (closed book):**
1. Why can reordering columns shrink a Postgres table without changing any data?
2. What are PostgreSQL's four TOAST strategies, and which is the default for `text`?
3. When does dictionary encoding stop paying off?
<details><summary>Answers</summary>

1. Fixed-width types are aligned to their size (int8 to 8 B), so small columns between large ones create padding; grouping by alignment removes it.
2. PLAIN, EXTENDED, EXTERNAL, MAIN; `text` defaults to EXTENDED (compress, then move out of line).
3. At high cardinality: the dictionary approaches the data size and the index codes add overhead (Parquet writers fall back to plain encoding when the dictionary page grows too large).
</details>

---

## 03 — Access Methods and Table Scans  ([chapter](03-access-methods-and-table-scans.md))
**Time:** ~3 h · **Needs:** Postgres

Setup for this chapter: `CREATE TABLE big AS SELECT g AS id, (random()*1e6)::int AS k, g AS seq, md5(g::text) AS s FROM generate_series(1,5000000) g; CREATE INDEX ON big(k); CREATE INDEX ON big(seq); VACUUM ANALYZE big;`

- [ ] **03.1 The seq-scan ring buffer and hint bits** *(Level: Core)*
  - **Goal:** See §2's ring-buffer strategy protect `shared_buffers`.
  - **Do:** Recreate `big` without the VACUUM, restart pg, then `EXPLAIN (ANALYZE, BUFFERS) SELECT count(*) FROM big;` and `SELECT count(*) FROM pg_buffercache WHERE relfilenode = pg_relation_filenode('big');`
  - **Predict:** How many of the table's ~47k pages stay in the 16k-page (128 MB) buffer pool, and whether a read-only scan dirties pages.
  - **Verify:** About 32 buffers per scanning process, and `dirtied=` close to the page count on the first scan. Explain the dirtying (hint bits, see [07](07-oltp-databases.md) §2).
- [ ] **03.2 Find the plan crossover** *(Level: Core)*
  - **Goal:** Measure §8's selectivity ranges.
  - **Do:** For N in 10, 100, 1k, 10k, 50k, 100k, 300k, 1M run `EXPLAIN (ANALYZE, BUFFERS) SELECT sum(id) FROM big WHERE k < N;` Record node type, time and buffers. Repeat with `SET random_page_cost = 1.1;`
  - **Predict:** The selectivity where Index Scan gives way to Bitmap, and where Bitmap gives way to Seq Scan.
  - **Verify:** A table showing both crossovers and how far they moved with `random_page_cost`.
- [ ] **03.3 Correlation decides** *(Level: Core)*
  - **Goal:** Isolate the index-correlation effect (§8).
  - **Do:** `SELECT attname, correlation FROM pg_stats WHERE tablename='big';` Then compare `WHERE seq < 50000` with `WHERE k < 10000` (both about 1%), using `EXPLAIN (ANALYZE, BUFFERS)`.
  - **Predict:** The plan and number of heap pages touched for each.
  - **Verify:** `seq` has correlation ≈ 1 and touches about 1% of pages; `k` has correlation ≈ 0 and touches most pages.
- [ ] **03.4 Lossy bitmaps and BitmapAnd** *(Level: Core)*
  - **Goal:** Make §4's exact-to-lossy switch visible.
  - **Do:** `SET work_mem='64kB'; EXPLAIN (ANALYZE, BUFFERS) SELECT count(*) FROM big WHERE k < 200000;` then with `work_mem='64MB'`. Then `WHERE k < 100000 AND seq < 1000000` for BitmapAnd.
  - **Predict:** Whether `Heap Blocks:` shows `lossy=` and how many `Rows Removed by Index Recheck` appear.
  - **Verify:** The lossy/exact block counts and recheck rows at both `work_mem` settings.
- [ ] **03.5 Index-only scans need the visibility map** *(Level: Core)*
  - **Goal:** Tie §3 index-only scans to the VM.
  - **Do:** `EXPLAIN (ANALYZE, BUFFERS) SELECT k FROM big WHERE k BETWEEN 1000 AND 2000;` Note `Heap Fetches`. `UPDATE big SET s = s WHERE id % 10 = 0;` and repeat. Then `VACUUM big;` and repeat. Check `SELECT * FROM pg_visibility_map_summary('big');` at each step.
  - **Predict:** Heap Fetches after each step.
  - **Verify:** 0, then high, then 0 again, with `all_visible` tracking the same pattern.
- [ ] **03.6 Parallel scan and Amdahl** *(Level: Stretch)*
  - **Goal:** Fit §7's Amdahl's law to your hardware.
  - **Do:** For `max_parallel_workers_per_gather` in 0, 1, 2, 4, 8, time `SELECT count(*) FROM big WHERE s LIKE '%ab%';` (warm cache, 3 runs each).
  - **Predict:** Speedup at 4 workers.
  - **Verify:** A speedup curve and the serial fraction `s` fitted from `S(n) = 1 / (s + (1-s)/n)`.

**Checkpoint (closed book):**
1. Why does a bitmap heap scan read pages in physical order, and what does it lose by doing so?
2. What two conditions must hold for an index-only scan to skip the heap?
3. What does `random_page_cost = 4` model, and why do people lower it on SSDs?
<details><summary>Answers</summary>

1. It collects TIDs into a page-ordered bitmap first, so each heap page is read once and sequentially. It loses index order (needs a Sort for ORDER BY) and, when lossy, must recheck every tuple on the page.
2. The index contains all referenced columns, and the heap page is marked all-visible in the visibility map.
3. A random page read costs 4× a sequential one (HDD era). SSD random reads are nearly as cheap as sequential, so 1.1–1.5 makes index plans compete fairly.
</details>

---

## 04 — Query Engine Internals  ([chapter](04-query-engine-internals.md))
**Time:** ~4 h · **Needs:** Postgres, Python + numpy

Setup: `CREATE TABLE cust AS SELECT g AS id, (g % 50) AS city, (g % 50) * 1000 + (g % 7) AS zip FROM generate_series(1,100000) g; CREATE TABLE ord AS SELECT g AS id, (random()*99999)::int + 1 AS cust_id, random()*100 AS amt FROM generate_series(1,2000000) g; ANALYZE;`

- [ ] **04.1 Force every join algorithm** *(Level: Core)*
  - **Goal:** Measure §5's decision matrix.
  - **Do:** `EXPLAIN (ANALYZE, BUFFERS) SELECT c.city, sum(o.amt) FROM ord o JOIN cust c ON c.id = o.cust_id GROUP BY 1;` Rerun with `enable_hashjoin=off`, then also `enable_mergejoin=off`. Add `CREATE INDEX ON cust(id)` and try a join restricted to 10 orders.
  - **Predict:** Rank the three algorithms by time for the big join, and which wins for 10 orders.
  - **Verify:** A table of algorithm, time and memory (`Buckets/Batches/Memory Usage` on Hash nodes).
- [ ] **04.2 Break the estimator with correlated columns** *(Level: Core)*
  - **Goal:** Reproduce §4.2's independence assumption failure and fix it (§4.6).
  - **Do:** `EXPLAIN ANALYZE SELECT * FROM cust WHERE city = 7 AND zip = 7000;` Compare estimated and actual rows. Then `CREATE STATISTICS cz (dependencies) ON city, zip FROM cust; ANALYZE cust;` and rerun.
  - **Predict:** The estimate before and after (hint: the selectivities are multiplied).
  - **Verify:** The estimate error factor goes from roughly 50× to about 1×.
- [ ] **04.3 Spill to disk** *(Level: Core)*
  - **Goal:** Watch §7.1, §7.2 and §7.5.
  - **Do:** With `work_mem='64kB'` and then `'256MB'`: `EXPLAIN ANALYZE SELECT * FROM ord ORDER BY amt;`, `EXPLAIN ANALYZE SELECT cust_id, count(*) FROM ord GROUP BY cust_id;`, and `EXPLAIN ANALYZE SELECT * FROM ord ORDER BY amt LIMIT 10;` Check `docker compose logs pg | grep temporary`.
  - **Predict:** The sort method and disk usage for each, and whether LIMIT 10 spills.
  - **Verify:** `external merge Disk: N kB` vs `quicksort`, HashAggregate `Batches:` > 1, and `top-N heapsort` for the LIMIT query regardless of `work_mem`.
- [ ] **04.4 Volcano vs vectorized** *(Level: Core)*
  - **Goal:** Measure the per-tuple overhead that §6.2 removes.
  - **Do:** In Python, implement `Scan → Filter(x > 0.5) → Sum` twice: as generator iterators yielding one tuple at a time (the `SeqScanOp`/`FilterOp` shape in [simpledb.py](simpledb.py)), and as operators passing numpy batches of 2,048. Run on 10M floats.
  - **Predict:** The speedup of the batch version.
  - **Verify:** ns per tuple for both, and the speedup at batch sizes 1, 64, 2,048 and 65,536.
- [ ] **04.5 The generic-plan trap** *(Level: Stretch)*
  - **Goal:** Reproduce §4.7's plan-caching failure.
  - **Do:** `CREATE TABLE jobs AS SELECT g AS id, CASE WHEN g % 1000 = 0 THEN 'queued' ELSE 'done' END AS status FROM generate_series(1,2000000) g; CREATE INDEX ON jobs(status); ANALYZE jobs; PREPARE q(text) AS SELECT count(*) FROM jobs WHERE status = $1;` Run `EXPLAIN EXECUTE q('queued')` six times, then `EXPLAIN ANALYZE EXECUTE q('done')`. Repeat with `SET plan_cache_mode = force_custom_plan;`.
  - **Predict:** On which execution the plan text switches to `$1`, and what that plan does for `'done'`.
  - **Verify:** The switch after 5 custom plans, and the timing difference for `'done'` between the two modes.

**Checkpoint (closed book):**
1. Why does a hash join build on the smaller input?
2. What does "Rows Removed by Filter" in EXPLAIN ANALYZE tell you that the estimate does not?
3. Name two costs of the Volcano model that vectorized execution removes.
<details><summary>Answers</summary>

1. The build side must fit in `work_mem` (or it goes multi-batch and spills); the probe side is only streamed.
2. How much work the scan did for rows it threw away, which points to a missing or unused index or a predicate that can't be pushed down.
3. A virtual `next()` call per tuple per operator, and poor cache/SIMD use from interpreting one row at a time (branch mispredictions, no tight loops).
</details>

---

## 05 — Transactions and Concurrency  ([chapter](05-transactions-and-concurrency.md))
**Time:** ~4 h · **Needs:** Postgres (two psql terminals), Python + psycopg

- [ ] **05.1 Fill in the anomaly matrix yourself** *(Level: Core)*
  - **Goal:** Reproduce §2 anomalies and §3 levels.
  - **Do:** With two sessions on `acct(id int primary key, bal int)`, reproduce non-repeatable read and phantom read at READ COMMITTED, then try both at REPEATABLE READ. Script each interleaving in your notebook before running.
  - **Predict:** Which anomalies Postgres REPEATABLE READ still allows (it is stronger than the SQL standard's).
  - **Verify:** A 2×2 table (anomaly × level) filled from observed output, not from the chapter.
- [ ] **05.2 Lost updates under load** *(Level: Core)*
  - **Goal:** Measure §2.4 and its fixes.
  - **Do:** Python: 10 threads × 1,000 iterations of `SELECT bal` → `UPDATE acct SET bal = <read + 1>` at READ COMMITTED. Then fix three ways: atomic `SET bal = bal + 1`, `SELECT ... FOR UPDATE`, and REPEATABLE READ with a retry loop on SQLSTATE `40001`.
  - **Predict:** The final balance of the broken version (expected 10,000).
  - **Verify:** The final balance, throughput and retry count for all four variants.
- [ ] **05.3 Write skew: REPEATABLE READ lets it through, SERIALIZABLE blocks it** *(Level: Core)*
  - **Goal:** Reproduce §2.5 and see §3.6 SSI stop it.
  - **Do:** `oncall(doctor text, on_call bool)` with two doctors on call. In each session: `BEGIN ISOLATION LEVEL REPEATABLE READ; SELECT count(*) FROM oncall WHERE on_call;` then each takes a different doctor off call and commits. Repeat at SERIALIZABLE and look at `SELECT locktype, relation::regclass, page, tuple, mode FROM pg_locks WHERE mode = 'SIReadLock';` before committing.
  - **Predict:** Final on-call count under each level, and which session gets the error.
  - **Verify:** 0 doctors at RR. At SERIALIZABLE, one commit fails with `could not serialize access due to read/write dependencies among transactions`.
- [ ] **05.4 A job queue with SKIP LOCKED** *(Level: Core)*
  - **Goal:** Measure §8.3 against naive locking.
  - **Do:** 100k rows in `jobs(id, status)`. 8 Python workers loop on `SELECT id FROM jobs WHERE status='queued' ORDER BY id LIMIT 10 FOR UPDATE SKIP LOCKED`, mark them done, and commit. Compare with the same without `SKIP LOCKED`. Log every id each worker processed.
  - **Predict:** The throughput ratio.
  - **Verify:** Jobs/s for both, and zero ids processed twice. Compare with the design in [job-scheduler-postgres-deep-dive.md](../solutions/job-scheduler-postgres-deep-dive.md).
- [ ] **05.5 Break it: one idle transaction stops cleanup** *(Level: Core)*
  - **Goal:** See §7.4 in the numbers.
  - **Do:** Session A: `BEGIN ISOLATION LEVEL REPEATABLE READ; SELECT 1;` and leave it. Session B: update every row of a 100k-row table 5 times, then `VACUUM (VERBOSE) that_table;` Find A in `SELECT pid, backend_xmin, state, xact_start FROM pg_stat_activity;`. Commit A and VACUUM again.
  - **Predict:** How many dead tuples the first VACUUM can remove.
  - **Verify:** The `dead but not yet removable` line in VACUUM output, then the drop to 0 after A commits. Name the setting that would have killed A (`idle_in_transaction_session_timeout`).
- [ ] **05.6 Explain it** *(Level: Stretch)*
  - **Goal:** Teach §8.5–8.6.
  - **Do:** Write 5 sentences to a backend team moving a payments service to SERIALIZABLE: why retries are mandatory, which errors to retry, and why the retried unit must be idempotent.
  - **Verify:** A peer (or you, a week later) can write the retry loop from your note alone.

**Checkpoint (closed book):**
1. Why does snapshot isolation allow write skew but prevent lost updates in Postgres?
2. What does SSI track to decide which transaction to abort?
3. What happens to a `SELECT ... FOR UPDATE SKIP LOCKED` that finds every candidate row locked?
<details><summary>Answers</summary>

1. Lost update means two writers hit the same row, and first-updater-wins aborts the second. Write skew means each writes a different row after reading an overlapping set, so there is no write-write conflict to detect.
2. rw-antidependencies via SIRead locks. It aborts when it finds two consecutive rw edges (a "dangerous structure") that could form a cycle.
3. It returns zero rows immediately instead of waiting.
</details>

---

## 06 — Indexing Internals  ([chapter](06-indexing-internals.md))
**Time:** ~4 h · **Needs:** Postgres, Python

- [ ] **06.1 Measure a B+tree's height** *(Level: Core)*
  - **Goal:** Check §2's fan-out and height math against a real index.
  - **Do:** `CREATE TABLE k AS SELECT g::int8 AS id FROM generate_series(1,10000000) g; CREATE INDEX k_id ON k(id);` Then `SELECT * FROM bt_metap('k_id');`, `SELECT * FROM bt_page_stats('k_id', <root>);`, `SELECT itemoffset, ctid, data FROM bt_page_items('k_id', 1) LIMIT 5;`
  - **Predict:** Entries per leaf page (16 B per item plus a 4 B line pointer; CREATE INDEX fills leaves to 90%), leaf count, and `level` of the root.
  - **Verify:** `bt_metap.level` and `pgstatindex('k_id')` leaf_pages match your math within 10%. Identify the high key on a leaf page.
- [ ] **06.2 Random keys cost WAL, not just space** *(Level: Core)*
  - **Goal:** Extend §2's UUID measurement with WAL volume, which the chapter does not show.
  - **Do:** Two tables: `id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY` and `id uuid PRIMARY KEY DEFAULT gen_random_uuid()`. For each: `SELECT pg_stat_reset_shared('wal');` then insert 3M rows in 30 batches with `CHECKPOINT` after every 10 batches. Read `wal_records, wal_fpi, wal_bytes` from `pg_stat_wal` and `avg_leaf_density` from `pgstatindex`.
  - **Predict:** The ratio of `wal_fpi` between the two.
  - **Verify:** A table of wal_bytes, wal_fpi, index size and leaf density. Explain the FPI gap in one sentence (random leaves are each touched again after every checkpoint).
- [ ] **06.3 Composite order and covering** *(Level: Core)*
  - **Goal:** Practice §2's composite-index rules.
  - **Do:** Table `ev(tenant int, ts timestamptz, kind int, payload text)` with 5M rows. Queries: Q1 `WHERE tenant=? AND ts > ?`, Q2 `WHERE ts > ?`, Q3 `SELECT kind FROM ev WHERE tenant=? AND ts > ?`. Try `(tenant, ts)`, `(ts, tenant)`, and `(tenant, ts) INCLUDE (kind)`.
  - **Predict:** Which index serves each query and which gives an index-only scan.
  - **Verify:** A 3×3 grid of plan type and shared buffers.
- [ ] **06.4 Build a Bloom filter** *(Level: Core)*
  - **Goal:** Verify §5's false-positive formula.
  - **Do:** Python with a `bytearray` bit array and double hashing (`h1 + i*h2` from one `hashlib.blake2b` digest). Insert 1M keys with m/n = 10 bits, then probe 1M absent keys for k = 1..10.
  - **Predict:** The best k and its false-positive rate from `(1 - e^(-kn/m))^k`.
  - **Verify:** Measured FPR vs formula for every k. The best k should be about 7 and the FPR about 0.8%.
- [ ] **06.5 BRIN, and how to break it** *(Level: Core)*
  - **Goal:** See §7.3's dependence on physical order.
  - **Do:** Two copies of 20M rows with a `ts` column: one inserted in time order, one `ORDER BY random()`. Create `USING brin (ts)` on both and a B-tree on one. Compare index sizes and `EXPLAIN (ANALYZE, BUFFERS)` for a one-day range.
  - **Predict:** BRIN size vs B-tree size, and buffers read on the shuffled copy.
  - **Verify:** BRIN is kilobytes vs hundreds of MB. On the shuffled copy BRIN reads nearly every block, and `Rows Removed by Index Recheck` shows it.
- [ ] **06.6 Text search three ways** *(Level: Stretch)*
  - **Goal:** Compare §7.1 GIN and §8 full-text search.
  - **Do:** 1M rows of generated sentences. Time `ILIKE '%word%'` (seq scan), then the same with `USING gin (body gin_trgm_ops)`, then `to_tsvector('english', body) @@ to_tsquery('word')` with a GIN expression index. Then time 100k inserts with and without each index.
  - **Predict:** The read speedups and the insert slowdown GIN causes.
  - **Verify:** A table of query time and insert time per index.

**Checkpoint (closed book):**
1. Why do B+trees keep all values in leaves and link the leaves?
2. When is a BRIN index worthless?
3. Two queries filter on `a` alone and on `a AND b`. What composite index serves both, and why not `(b, a)`?
<details><summary>Answers</summary>

1. Internal nodes then hold only keys, so fan-out is higher and the tree is shorter; linked leaves make range scans a sequential walk without going back up.
2. When the column's values are not correlated with physical row order, so every block range's min/max covers the whole domain.
3. `(a, b)`. The leftmost prefix `a` serves both. `(b, a)` cannot seek on `a` alone (at best a skip scan).
</details>

---

## 07 — OLTP Databases  ([chapter](07-oltp-databases.md))
**Time:** ~4 h · **Needs:** Postgres, MySQL 8.4 (`docker compose --profile mysql up -d`), sqlite3

- [ ] **07.1 HOT updates and fillfactor** *(Level: Core)*
  - **Goal:** Watch §2's HOT mechanism work and then fail.
  - **Do:** Two tables of 100k rows `(id int primary key, v int, note text)`, one `WITH (fillfactor=100)`, one `WITH (fillfactor=80)`. Run `UPDATE ... SET note = note || 'x'` on all rows 3 times. Read `n_tup_upd, n_tup_hot_upd` from `pg_stat_user_tables`, plus index sizes. Inspect a page: `SELECT lp, lp_flags, t_ctid, (t_infomask2 & 16384) > 0 AS hot_updated, (t_infomask2 & 32768) > 0 AS heap_only FROM heap_page_items(get_raw_page('t80', 0));`
  - **Predict:** The HOT ratio for each fillfactor.
  - **Verify:** A higher HOT ratio at fillfactor 80, and redirect line pointers (`lp_flags = 2`) after pruning. **Break it:** `CREATE INDEX ON t80(note);`, update again, and watch the HOT ratio fall to 0.
- [ ] **07.2 Bloat, VACUUM and VACUUM FULL** *(Level: Core)*
  - **Goal:** Measure §2 Vacuum.
  - **Do:** A 1M-row table `WITH (autovacuum_enabled = false)`. Update all rows 3 times. Record `pg_relation_size` and `SELECT dead_tuple_percent, free_percent FROM pgstattuple('t')`. Run `VACUUM`, measure, then `VACUUM FULL` while another session runs `SELECT count(*)` in a loop.
  - **Predict:** Table size after each step, and whether the reader blocks.
  - **Verify:** VACUUM leaves the size unchanged but raises `free_percent`. VACUUM FULL shrinks it and blocks the reader (ACCESS EXCLUSIVE lock, visible in `pg_locks`).
- [ ] **07.3 InnoDB clustered index and key order** *(Level: Core)*
  - **Goal:** Feel §3's clustered storage.
  - **Do:** In MySQL: tables with PK `BIGINT AUTO_INCREMENT`, `CHAR(36)` filled by `UUID()`, and `BINARY(16)` filled by `UUID_TO_BIN(UUID(), 1)`, each with `pad CHAR(100)`. Insert 1M rows each with `SET SESSION cte_max_recursion_depth = 1000000; INSERT INTO a (pad) WITH RECURSIVE s(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM s WHERE n < 1000000) SELECT 'x' FROM s;` Then `ANALYZE TABLE` and read `data_length` from `information_schema.TABLES`.
  - **Predict:** Insert time and size ranking.
  - **Verify:** A table of time and data_length. Explain why the swap flag in `UUID_TO_BIN(..., 1)` changes the result.
- [ ] **07.4 InnoDB's undo history vs Postgres dead tuples** *(Level: Core)*
  - **Goal:** Compare §3 InnoDB MVCC with 05.5.
  - **Do:** Session A: `START TRANSACTION WITH CONSISTENT SNAPSHOT; SELECT count(*) FROM a;` Session B: update all 1M rows twice. Check `SHOW ENGINE INNODB STATUS\G` for `History list length`, then commit A and check again after 30 s.
  - **Predict:** History list length while A is open.
  - **Verify:** The number grows into the millions and drains after A commits. Write two sentences on where old versions live in InnoDB versus Postgres.
- [ ] **07.5 SQLite: rollback journal vs WAL** *(Level: Core)*
  - **Goal:** Measure §7's journaling modes.
  - **Do:** Python `sqlite3`: 10k single-row transactions under `PRAGMA journal_mode=DELETE` and `=WAL`, each with `synchronous=FULL` and `NORMAL`. Then hold a read transaction open in one connection while another tries to commit a write.
  - **Predict:** Commits/s for the four combinations, and which mode lets the writer commit during the open read.
  - **Verify:** A 2×2 throughput table. The rollback journal writer gets `database is locked`, and the WAL writer succeeds.
- [ ] **07.6 What connections cost** *(Level: Stretch)*
  - **Goal:** Quantify §8's argument for pooling.
  - **Do:** `pgb -i -s 20 lab`, then `pgb -S -c 16 -j 4 -T 30 lab` with and without `-C` (a new connection per transaction).
  - **Predict:** The TPS ratio.
  - **Verify:** Both TPS numbers and the per-connection setup cost in ms derived from them.

**Checkpoint (closed book):**
1. What two conditions make an UPDATE eligible for HOT?
2. Why can a random primary key hurt InnoDB more than Postgres?
3. Why does SQLite WAL mode let readers and a writer run concurrently?
<details><summary>Answers</summary>

1. No indexed column changes, and the new version fits on the same heap page.
2. InnoDB stores rows inside the PK B+tree, so random keys split and scatter the whole table, not just an index; every secondary index also stores the (wide) PK.
3. The writer appends to the WAL file while readers read the database file plus the WAL up to their snapshot's end mark; nobody overwrites pages a reader needs until a checkpoint.
</details>

---

## 08 — OLAP Databases  ([chapter](08-olap-databases.md))
**Time:** ~4 h · **Needs:** Postgres, DuckDB, chDB

- [ ] **08.1 DuckDB vs Postgres on TPC-H Q1** *(Level: Core)*
  - **Goal:** Measure §2–§3 (columnar, vectorized) against a row store on the same data.
  - **Do:** In DuckDB: `INSTALL tpch; LOAD tpch; CALL dbgen(sf=1); COPY lineitem TO 'lineitem.csv' (HEADER);` Create `lineitem` in Postgres with matching types and `\copy` it in, then `VACUUM ANALYZE`. Run Q1 (`SELECT query FROM tpch_queries() WHERE query_nr = 1` in DuckDB) in both, 3 warm runs each.
  - **Predict:** The DuckDB speedup, and the size of each copy on disk.
  - **Verify:** Times, Postgres `EXPLAIN (ANALYZE, BUFFERS)` showing full-table buffers read, the DuckDB `EXPLAIN ANALYZE` profile, and `pg_total_relation_size` vs the `.duckdb` file size.
- [ ] **08.2 ClickHouse's sparse primary index** *(Level: Core)*
  - **Goal:** See §4's granules.
  - **Do:** chDB session: `CREATE TABLE hits (user_id UInt32, ts DateTime, url String) ENGINE = MergeTree ORDER BY (user_id, ts);` Insert 50M rows from `numbers()`. Run `EXPLAIN indexes = 1 SELECT count() FROM hits WHERE user_id = 42;` and the same filtering only on `ts`.
  - **Predict:** Granules selected out of the total for each query (8,192 rows per granule).
  - **Verify:** The `Granules: x/y` lines. Then `ALTER TABLE hits ADD INDEX ts_mm ts TYPE minmax GRANULARITY 1; ALTER TABLE hits MATERIALIZE INDEX ts_mm;` and show the `ts` query skipping granules.
- [ ] **08.3 Pre-aggregation, and the unmerged-rows trap** *(Level: Core)*
  - **Goal:** Build §4's materialized-view pattern.
  - **Do:** Create a `SummingMergeTree ORDER BY (user_id, day)` target and a `MATERIALIZED VIEW ... TO` it that aggregates `hits`. Insert more data in several batches. Query the target with and without `GROUP BY`/`sum()`, and with `FINAL`.
  - **Predict:** Whether a plain `SELECT *` on the target returns one row per key.
  - **Verify:** Duplicate keys before background merges, and correct totals with `sum() ... GROUP BY` or `FINAL`. Record the query-time speedup over raw `hits`.
- [ ] **08.4 Approximate answers** *(Level: Core)*
  - **Goal:** Measure §12's approximate query processing.
  - **Do:** In DuckDB on `lineitem`: `count(DISTINCT l_partkey)` vs `approx_count_distinct(l_partkey)`, and `quantile_cont(l_extendedprice, 0.99)` vs `approx_quantile(l_extendedprice, 0.99)`.
  - **Predict:** The relative error and the speedup.
  - **Verify:** A table of exact vs approximate value, error % and time.
- [ ] **08.5 Star schema vs one wide table** *(Level: Stretch)*
  - **Goal:** Test §1's star schema and §12's denormalization trade-off.
  - **Do:** Using TPC-H `lineitem`, `orders`, `customer`, `nation`, time a revenue-by-nation query as a join, then build a denormalized wide table with CTAS and query it.
  - **Predict:** The query speedup and the storage cost of the wide table.
  - **Verify:** Both times and both sizes. State one condition under which you would keep the star schema anyway.

**Checkpoint (closed book):**
1. Why does ClickHouse use a sparse index instead of a B-tree?
2. What is late materialization and why does it save work?
3. Why is `SELECT *` especially expensive in a column store?
<details><summary>Answers</summary>

1. Data is sorted by the key, so one index entry per 8,192-row granule is enough to binary-search ranges. The index stays small enough to keep in memory, and analytical queries read ranges, not single rows.
2. Filter on a few columns first, keep row positions, and fetch the other columns only for surviving rows, so most rows are never decoded in the other columns.
3. Every column is a separate file or stream, so reading all columns means reading and decompressing all of them and stitching rows back together.
</details>

---

## 09 — HTAP Databases  ([chapter](09-htap-databases.md))
**Time:** ~3 h · **Needs:** Postgres + replica (`docker compose --profile repl up -d`), DuckDB; TiDB optional

- [ ] **09.1 The noisy neighbour, measured** *(Level: Core)*
  - **Goal:** Put a number on §9's resource contention.
  - **Do:** `pgb -i -s 50 lab`. Baseline: `pgb -c 8 -j 4 -T 60 -P 5 lab`. Then rerun while 4 psql loops execute `SELECT aid % 1000, sum(abalance) FROM pgbench_accounts GROUP BY 1 ORDER BY 2 DESC;` back to back.
  - **Predict:** TPS drop and p99 latency growth.
  - **Verify:** Baseline vs contended TPS and average latency from pgbench's summary, plus the worst 5 s interval from `-P 5`.
- [ ] **09.2 Does a big scan evict the OLTP working set?** *(Level: Core)*
  - **Goal:** Test §9's buffer-pool-pressure claim against Postgres's ring buffer ([03](03-access-methods-and-table-scans.md) §2).
  - **Do:** While pgbench runs, snapshot `SELECT c.relname, count(*) FROM pg_buffercache b JOIN pg_class c ON b.relfilenode = pg_relation_filenode(c.oid) GROUP BY 1 ORDER BY 2 DESC LIMIT 5;` Run the analytical scan. Snapshot again. Then run an analytical query that uses a large hash join with `work_mem='1GB'` and snapshot.
  - **Predict:** Whether `pgbench_accounts_pkey` buffers survive each query.
  - **Verify:** Before/after buffer counts. Explain which analytical work the ring buffer does not protect against (CPU, I/O bandwidth, `work_mem`).
- [ ] **09.3 Break it: analytics on a replica** *(Level: Core)*
  - **Goal:** See §9's consistency-vs-freshness trade-off as a real error.
  - **Do:** On the replica (port 5433): `SELECT pg_sleep(120), (SELECT count(*) FROM pgbench_history);` While it runs, on the primary: `DELETE FROM pgbench_history; VACUUM pgbench_history;`. Wait. Then on the replica `ALTER SYSTEM SET hot_standby_feedback = on; SELECT pg_reload_conf();` and repeat, running `VACUUM VERBOSE pgbench_history` on the primary.
  - **Predict:** What happens to the replica query after about 30 s (`max_standby_streaming_delay`) in each mode.
  - **Verify:** `canceling statement due to conflict with recovery` and a non-zero `confl_snapshot` or `confl_lock` in `pg_stat_database_conflicts` on the replica. With feedback on, the query survives and the primary reports rows it cannot remove.
- [ ] **09.4 Offload to a columnar copy** *(Level: Core)*
  - **Goal:** Build the §2 dual-format pattern by hand and measure freshness.
  - **Do:** DuckDB: `INSTALL postgres; LOAD postgres; ATTACH 'host=localhost port=5432 user=postgres password=lab dbname=lab' AS pg (TYPE postgres, READ_ONLY);` Time the 09.1 aggregate against `pg.public.pgbench_accounts`, then `CREATE TABLE snap AS SELECT * FROM pg.public.pgbench_accounts;` and time it against `snap`. Refresh the snapshot every 60 s while pgbench runs.
  - **Predict:** Query speedup on the snapshot, and the worst-case staleness.
  - **Verify:** Both times, the refresh cost, and a staleness figure (seconds) for your refresh interval.
- [ ] **09.5 TiFlash replica** *(Level: Stretch, optional)*
  - **Goal:** See §3's learner-replica HTAP on a real engine.
  - **Do:** `tiup playground --tiflash 1`. Create and load a table, then `ALTER TABLE t SET TIFLASH REPLICA 1;` Wait until `SELECT progress FROM information_schema.tiflash_replica` reaches 1. Compare `EXPLAIN ANALYZE` of an aggregate with `SET SESSION tidb_isolation_read_engines = 'tikv';` and `'tiflash';`.
  - **Predict:** The speedup and the task type in the plan.
  - **Verify:** `mpp[tiflash]` or `cop[tiflash]` tasks in the plan, and both timings.

**Checkpoint (closed book):**
1. Name the four HTAP architecture patterns from §2.
2. Why does `hot_standby_feedback = on` stop query cancellations, and what does it cost?
3. Give two workload signals that favour separate OLTP and OLAP systems over HTAP.
<details><summary>Answers</summary>

1. Dual-format storage, in-memory with columnar, distributed SQL with analytics extensions, cloud-native HTAP.
2. The standby reports its oldest snapshot xmin to the primary, so vacuum keeps the rows the standby still needs. The cost is bloat on the primary.
3. Any two of: analytics tolerates minutes-to-hours staleness; heavy ad-hoc scans that would starve OLTP; very different scaling or cost profiles; analytics joins data from many sources.
</details>

---

## 10 — In-Memory Databases  ([chapter](10-in-memory-databases.md))
**Time:** ~3 h · **Needs:** Redis 7.4 (`docker compose --profile redis up -d`), Python + redis

`alias rc='docker compose exec redis redis-cli'`

- [ ] **10.1 Compact encodings and the conversion cliff** *(Level: Core)*
  - **Goal:** See §2's listpack-to-hashtable switch.
  - **Do:** `for i in $(seq 1 128); do echo "HSET h128 f$i v$i"; done | docker compose exec -T redis redis-cli > /dev/null`, and the same for `h129` with 129 fields. Then `rc OBJECT ENCODING h128`, `rc OBJECT ENCODING h129`, `rc MEMORY USAGE h128`, `rc MEMORY USAGE h129`.
  - **Predict:** Each encoding and the memory ratio between the two keys.
  - **Verify:** `listpack` vs `hashtable` and the memory jump for one extra field. Relate it to `hash-max-listpack-entries`.
- [ ] **10.2 What durability costs** *(Level: Core)*
  - **Goal:** Measure §2's persistence options.
  - **Do:** `docker compose exec redis redis-benchmark -t set -n 1000000 -P 16 -q` with `CONFIG SET appendonly no`, then `appendonly yes` with `appendfsync everysec`, then `appendfsync always`.
  - **Predict:** The SET/s ratio of the three.
  - **Verify:** Three numbers. State the worst-case data loss window of each.
- [ ] **10.3 fork() and copy-on-write** *(Level: Core)*
  - **Goal:** Measure §2's snapshot cost.
  - **Do:** `rc DEBUG POPULATE 2000000 key 500`, then `rc BGSAVE` while `redis-benchmark -t set -r 2000000 -d 500 -n 2000000 -P 16` writes keys. Read `latest_fork_usec` (INFO stats) and `rdb_last_cow_size` (INFO persistence).
  - **Predict:** Fork time for about 1.5 GB of data, and the COW size under heavy writes vs no writes.
  - **Verify:** Both numbers for the idle and the busy case. Size the RAM headroom you'd need for BGSAVE in production.
- [ ] **10.4 Approximated LRU** *(Level: Core)*
  - **Goal:** Test §2's sampled eviction.
  - **Do:** `CONFIG SET maxmemory 100mb`, `maxmemory-policy allkeys-lru`. From Python, issue 2M GETs with Zipf(1.1)-distributed keys over 1M possible keys, SETting 1 KB on a miss. Repeat with `maxmemory-samples` 1, 5 and 10, and with `allkeys-random`.
  - **Predict:** Hit ratio of each configuration.
  - **Verify:** Hit ratio from `keyspace_hits / (keyspace_hits + keyspace_misses)` and `evicted_keys` for each run (`CONFIG RESETSTAT` between runs).
- [ ] **10.5 Break it: cache stampede** *(Level: Stretch)*
  - **Goal:** Reproduce §9's thundering herd and fix it.
  - **Do:** 200 Python threads read one key with a 1 s TTL. A miss calls a fake DB (`time.sleep(0.2)`) and counts calls. Run for 30 s with plain cache-aside, then with a `SET lock:key 1 NX PX 500` single-flight guard, then with probabilistic early expiration.
  - **Predict:** DB calls per expiry for each strategy.
  - **Verify:** A table of total DB calls and p99 read latency.

**Checkpoint (closed book):**
1. Why can Redis do a consistent snapshot without stopping writes?
2. Why does Redis sample keys for LRU instead of keeping a linked list?
3. What is lost on crash with `appendfsync everysec`?
<details><summary>Answers</summary>

1. `fork()` gives the child a copy-on-write view of memory frozen at the fork; the parent keeps writing and only touched pages get copied.
2. A true LRU list costs two pointers per key and list updates on every access; sampling uses a 24-bit clock per object and approximates LRU well with 5–10 samples.
3. Up to about one second of acknowledged writes (more if the fsync falls behind).
</details>

---

## 11a — Vector Search Internals: IVF, PQ, Quantization  ([chapter](11-vector-search-internals.md))
**Time:** ~4 h · **Needs:** Python + faiss-cpu, numpy

Data: 1M × 128 float32 vectors from 1,000 Gaussian clusters (or SIFT1M if you can download it), plus 1,000 held-out queries. Ground truth: top-10 from `faiss.IndexFlatL2`.

- [ ] **11a.1 Brute-force baseline** *(Level: Core)*
  - **Goal:** Know what §1's exact search costs before approximating it.
  - **Do:** Time `IndexFlatL2.search(q, 10)` for 1,000 queries, single-threaded (`faiss.omp_set_num_threads(1)`) and all cores.
  - **Predict:** QPS and memory (N × d × 4 B).
  - **Verify:** QPS for both thread counts and the ground-truth file saved for later tasks.
- [ ] **11a.2 The nprobe curve** *(Level: Core)*
  - **Goal:** Measure §2.4's recall/latency trade-off.
  - **Do:** `IndexIVFFlat(IndexFlatL2(d), d, 4096)`, train on 100k vectors, add all. Sweep `index.nprobe` over 1, 4, 16, 64, 256.
  - **Predict:** The nprobe that first reaches recall@10 ≥ 0.9.
  - **Verify:** A recall@10 vs QPS table. **Break it:** train on 100k vectors from only 10 clusters, rebuild, and show the recall collapse and skewed `index.invlists.list_size(i)` distribution.
- [ ] **11a.3 PQ: memory vs recall, and re-ranking** *(Level: Core)*
  - **Goal:** Measure §3's compression and §6.3's two-phase retrieval.
  - **Do:** `IndexIVFPQ(quantizer, d, 4096, m, 8)` for m = 8, 16, 32. Then wrap the m=16 index in `IndexRefineFlat` with `k_factor=10`.
  - **Predict:** Bytes per vector for each m (`index.code_size`) and recall@10.
  - **Verify:** A table of bytes/vector, compression ratio vs 512 B, and recall@10 with and without re-ranking.
- [ ] **11a.4 Scalar and binary quantization** *(Level: Core)*
  - **Goal:** Compare §6.1 and §6.2.
  - **Do:** `IndexScalarQuantizer(d, faiss.ScalarQuantizer.QT_8bit)` vs flat. For binary: sign-threshold each dimension, pack with `np.packbits`, search with `IndexBinaryFlat` (Hamming) for top-100, then re-rank with exact L2.
  - **Predict:** Recall@10 for SQ8, binary alone, and binary + re-rank.
  - **Verify:** Recall and memory for all three.
- [ ] **11a.5 OPQ rotation** *(Level: Stretch)*
  - **Goal:** Test §4's claim.
  - **Do:** `faiss.index_factory(d, "IVF4096,PQ16")` vs `"OPQ16,IVF4096,PQ16"` at equal nprobe.
  - **Predict:** Recall gain from the rotation on your data.
  - **Verify:** Both recalls. Explain why the gain depends on how correlated your dimensions are.

**Checkpoint (closed book):**
1. What does nprobe trade, and why can't a query's true neighbour be found if it sits in an unprobed cell?
2. Why is asymmetric distance computation (ADC) more accurate than symmetric (SDC)?
3. Why do quantized indexes usually re-rank a shortlist with full vectors?
<details><summary>Answers</summary>

1. Recall against latency: more probed cells means more vectors scanned. IVF only scans the lists of the probed centroids, so a neighbour just across a Voronoi boundary is invisible.
2. ADC compares the exact query to quantized database vectors, so only the database side carries quantization error.
3. Compressed distances reorder near-ties. Fetching the top 10k candidates and scoring them exactly restores most of the recall at a small cost.
</details>

---

## 11b — HNSW Internals  ([chapter](11-hnsw-vector-search-internals.md))
**Time:** ~3 h · **Needs:** Python + faiss-cpu, Postgres with pgvector

- [ ] **11b.1 Check the level distribution** *(Level: Core)*
  - **Goal:** Verify §4's exponential level assignment.
  - **Do:** Build `faiss.IndexHNSWFlat(d, 16)` on 1M vectors. `levels = faiss.vector_to_array(index.hnsw.levels) - 1; np.bincount(levels)`.
  - **Predict:** The share of nodes at level ≥ 1, ≥ 2, and the max level (mL = 1/ln M).
  - **Verify:** About 1/16 at level ≥ 1, 1/256 at level ≥ 2, and max level ≈ log16(1M) ≈ 5.
- [ ] **11b.2 The efSearch knee** *(Level: Core)*
  - **Goal:** Measure §7.3.
  - **Do:** Sweep `index.hnsw.efSearch` over 16, 32, 64, 128, 256, 512 and measure recall@10 and QPS against the 11a ground truth.
  - **Predict:** The efSearch that first reaches recall 0.95.
  - **Verify:** A recall/QPS curve with the knee marked.
- [ ] **11b.3 M and efConstruction** *(Level: Core)*
  - **Goal:** Check §9.2's memory formula and §7.1–7.2.
  - **Do:** Build with M = 8, 16, 32, 64 (efConstruction 40 and 200). Record build time, `faiss.serialize_index(index).nbytes`, and recall at efSearch 64.
  - **Predict:** Bytes per vector for M=32 from §9.2.
  - **Verify:** Measured vs predicted memory within 15%, and the build-time cost of efConstruction 200.
- [ ] **11b.4 Break it: filtered search returns too few rows** *(Level: Core)*
  - **Goal:** Reproduce the filtering limitation from §12.3 in pgvector.
  - **Do:** `CREATE TABLE items (id bigserial, cat int, embedding vector(128));` Load 200k rows with `cat` from 0 to 99. `CREATE INDEX ON items USING hnsw (embedding vector_l2_ops) WITH (m = 16, ef_construction = 64);` Run `SELECT id FROM items WHERE cat = 7 ORDER BY embedding <-> '<q>' LIMIT 10;` with default `hnsw.ef_search = 40`. Then `SET hnsw.iterative_scan = relaxed_order;` and rerun.
  - **Predict:** Rows returned before the fix.
  - **Verify:** Fewer than 10 rows, often 0, then 10. Name one more fix (a partial index per hot category, or raising `hnsw.ef_search`).
- [ ] **11b.5 Build HNSW-lite** *(Level: Stretch)*
  - **Goal:** Retrieve §3 and §6 by implementing them.
  - **Do:** Python: layered insert with greedy descent (§3.1), `SEARCH-LAYER` (§3.3), and both simple (§6.1) and heuristic (§6.2) neighbour selection. Use 50k clustered vectors.
  - **Predict:** Recall gap between the two selection rules at the same M and ef.
  - **Verify:** Recall@10 for both, within a few points of faiss at the same parameters.

**Checkpoint (closed book):**
1. Why does HNSW search the upper layers with ef = 1?
2. What does the heuristic neighbour selection prevent?
3. Why is deleting from an HNSW graph hard?
<details><summary>Answers</summary>

1. Upper layers only route the search toward the right region; one greedy path is enough, and the beam width is spent at layer 0 where recall is decided.
2. Clustered neighbourhoods: it skips candidates that are closer to an already chosen neighbour than to the node, keeping long-range edges that bridge clusters.
3. Removing a node breaks paths through it. Neighbours must be relinked or the node tombstoned, and tombstones degrade recall until the graph is rebuilt.
</details>

---

## 12 — Replication and Distributed Storage  ([chapter](12-replication-and-distributed-storage.md))
**Time:** ~4 h · **Needs:** Postgres + replica (`docker compose --profile repl up -d`), Python

Also do §11.4 (the failure drill) against the setup below. The tasks here are different.

- [ ] **12.1 Replication lag under load** *(Level: Core)*
  - **Goal:** Measure §2.1 single-leader lag.
  - **Do:** Run `pgb -c 8 -T 120 lab`. Every 5 s on the primary: `SELECT application_name, sync_state, pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn) AS lag_bytes, write_lag, flush_lag, replay_lag FROM pg_stat_replication;` On the replica: `SELECT now() - pg_last_xact_replay_timestamp();`
  - **Predict:** Typical lag in bytes and ms.
  - **Verify:** A time series of the three lag columns. Explain why `write_lag < flush_lag < replay_lag`.
- [ ] **12.2 Break it: read-your-writes on a replica** *(Level: Core)*
  - **Goal:** Reproduce the §9.4 session-guarantee violation.
  - **Do:** Python: 10,000 times, INSERT a row on the primary, commit, then immediately SELECT it by id on the replica. Count misses (with pgbench load running). Fix it: after commit read `pg_current_wal_lsn()` on the primary, and on the replica wait until `pg_last_wal_replay_lsn() >= that_lsn` before reading.
  - **Predict:** Miss rate without the fix.
  - **Verify:** A non-zero miss rate, then 0, and the added p99 read latency of the fix.
- [ ] **12.3 Synchronous replication, then pause the standby** *(Level: Core)*
  - **Goal:** Feel §2.1's sync/async trade-off.
  - **Do:** `ALTER SYSTEM SET synchronous_standby_names = '*'; SELECT pg_reload_conf();` Compare `pgb -c 8 -T 30 -P 1` latency with the async run. Then `docker compose pause pg-replica` during a run. Then in psql: `SET synchronous_commit = local; INSERT ...;`. Unpause.
  - **Predict:** The latency increase, and what pgbench prints while the standby is paused.
  - **Verify:** Higher latency, then `0.0 tps` progress lines while paused. The local-commit insert returns immediately.
- [ ] **12.4 Break it: lose an acknowledged write in failover** *(Level: Core)*
  - **Goal:** Reproduce §11.4 scenario 4.
  - **Do:** Async mode (`synchronous_standby_names = ''`). `docker compose pause pg-replica`. INSERT 1,000 rows on the primary and note that every commit returned. `docker compose kill pg`, `docker compose unpause pg-replica`, then on the replica `SELECT pg_promote();` and count the rows.
  - **Predict:** How many of the 1,000 acknowledged rows survive.
  - **Verify:** 0. Write the one-line rule you would put in a runbook. Restore with `docker compose up -d pg` and `docker compose --profile repl up -d --force-recreate pg-replica`.
- [ ] **12.5 Consistent hashing vs mod-N** *(Level: Core)*
  - **Goal:** Measure §4.2 and §4.4 rebalancing.
  - **Do:** Python: 1M keys over 10 nodes. Count keys that move when an 11th node joins, with `hash(k) % N` and with a hash ring using 1, 16 and 256 virtual nodes per node. Also report the max/mean load per node.
  - **Predict:** Fraction moved for each scheme.
  - **Verify:** About 91% for mod-N and about 1/11 for the ring, with load imbalance shrinking as vnodes grow.
- [ ] **12.6 Quorum overlap by simulation** *(Level: Stretch)*
  - **Goal:** Check §2.3 and §8.4 quorum math.
  - **Do:** Simulate N=3 and N=5 leaderless replicas with random replica choice per request, message delay, and one replica down. Count stale reads for (W, R) in (1,1), (2,1), (2,2), (3,1).
  - **Predict:** Which configurations can return stale data.
  - **Verify:** Zero stale reads exactly when W + R > N (no sloppy quorums).

**Checkpoint (closed book):**
1. What does PACELC add to CAP?
2. With async replication, why can a client hold an acknowledgement for a write that no longer exists?
3. Why does consistent hashing need virtual nodes?
<details><summary>Answers</summary>

1. Else (no partition), the system still trades Latency against Consistency.
2. The leader acknowledged after its local commit and died before shipping WAL; the promoted replica never had it.
3. With few points per node, arcs are uneven (load skew), and a leaving node dumps its whole range onto one neighbour; many vnodes spread both evenly.
</details>

---

## 13 — LSM Trees and Compaction  ([chapter](13-lsm-trees-and-compaction.md))
**Time:** ~4 h · **Needs:** Python + rocksdict

- [ ] **13.1 Build a tiny LSM and measure write amplification** *(Level: Core)*
  - **Goal:** Derive §5's numbers from your own code.
  - **Do:** Python (extend `LSMTree` in [simpledb.py](simpledb.py) or write your own): dict memtable flushed at 4 MB to sorted SSTable files; leveled compaction with fanout 10, and size-tiered compaction merging 4 similar-size runs. Count every byte written to SSTables. Load 2M random 16 B keys with 100 B values, 50% overwrites.
  - **Predict:** Write amplification for leveled and tiered.
  - **Verify:** WA, read amplification (SSTables probed per GET, no Bloom filter), and space amplification (bytes on disk / live bytes) for both strategies.
- [ ] **13.2 Read RocksDB's own accounting** *(Level: Core)*
  - **Goal:** Compare 13.1 with a production engine (§6).
  - **Do:** `Options(raw_mode=True)` with `set_write_buffer_size(1 << 20)`, `set_target_file_size_base(1 << 20)`, `set_max_bytes_for_level_base(4 << 20)`. Put 2M random keys, `db.flush()`, then `print(db.property_value("rocksdb.stats"))`. Also read `write_bytes` from `/proc/self/io` before and after. Repeat with `set_compaction_style(DBCompactionStyle.universal())`.
  - **Predict:** The `W-Amp` value in the `Sum` row for each style.
  - **Verify:** RocksDB W-Amp, the `/proc` ratio (which also includes the WAL), and the level/file layout for both styles.
- [ ] **13.3 Bloom filters on the read path** *(Level: Core)*
  - **Goal:** Measure §3's point-lookup cost for missing keys.
  - **Do:** Load 1M keys. Time 200k GETs of absent keys, first without a filter, then with `BlockBasedOptions().set_bloom_filter(10, False)` passed through `set_block_based_table_factory`. Call `opt.enable_statistics()` and read `opt.get_statistics()`.
  - **Predict:** The speedup for absent keys.
  - **Verify:** Both timings and the `rocksdb.bloom.filter.useful` counter.
- [ ] **13.4 Break it: the tombstone graveyard** *(Level: Core)*
  - **Goal:** See §2's tombstone cost.
  - **Do:** Load 1M sequential keys, delete the first 900k one by one, then time an iterator that seeks to the first key and reads 10 entries. Run `db.compact_range(None, None)` and time again. Repeat with one `delete_range` call instead of point deletes.
  - **Predict:** Scan time before and after compaction.
  - **Verify:** Three timings. Explain why the first scan is slow even though it returns only 10 rows.
- [ ] **13.5 Provoke a write stall** *(Level: Stretch)*
  - **Goal:** Watch §6's write stalls.
  - **Do:** `set_max_background_jobs(1)`, a 1 MB write buffer, `set_level_zero_slowdown_writes_trigger(4)`, `set_level_zero_stop_writes_trigger(8)`. Write random keys as fast as possible for 60 s. A second thread samples `rocksdb.num-files-at-level0`, `rocksdb.actual-delayed-write-rate` and `rocksdb.is-write-stopped` every 100 ms.
  - **Predict:** Whether throughput is smooth or sawtooth.
  - **Verify:** A plot of writes/s against L0 file count, with stall periods marked.

**Checkpoint (closed book):**
1. State the RUM conjecture in one sentence.
2. Why does leveled compaction have higher write amplification but lower space amplification than tiered?
3. Why are L0 files allowed to overlap when L1+ files are not?
<details><summary>Answers</summary>

1. You can optimize at most two of read, update (write) and memory (space) overhead; improving one costs another.
2. Leveled rewrites a key into each level's sorted run (about fanout/2 per level), but keeps one version per level. Tiered writes each run once per tier and leaves several overlapping copies until they merge.
3. L0 files are raw memtable flushes, each covering the whole key range. Merging them on flush would put compaction on the write path.
</details>

---

## 14 — Write-Ahead Log Internals  ([chapter](14-write-ahead-log-internals.md))
**Time:** ~4 h · **Needs:** Postgres (`pg_walinspect`), Python

- [ ] **14.1 WAL bytes per transaction, and the full-page-write tax** *(Level: Core)*
  - **Goal:** Measure §3's record anatomy and FPWs.
  - **Do:** `SELECT pg_current_wal_insert_lsn() AS a \gset`, insert one row into a table with a primary key, `SELECT pg_current_wal_insert_lsn() AS b \gset`, then `SELECT pg_wal_lsn_diff(:'b', :'a');` and `SELECT "resource_manager/record_type", count, fpi_size, combined_size FROM pg_get_wal_stats(:'a', :'b', true) WHERE count > 0;` Then `CHECKPOINT;` and update one row. Update the same row again.
  - **Predict:** Bytes for the insert, and for the first and second update after the checkpoint.
  - **Verify:** About 150–200 B, then about 10–20 KB, then about 200 B. `pg_get_wal_records_info(:'a', :'b')` shows `fpi_length` on the first update only.
- [ ] **14.2 Group commit** *(Level: Core)*
  - **Goal:** See §4's flush batching.
  - **Do:** `SELECT pg_stat_reset_shared('wal');` then `pgb -c 1 -T 30 lab` and read `wal_sync` from `pg_stat_wal`. Repeat with `-c 32 -j 8`. Then `ALTER SYSTEM SET synchronous_commit = off; SELECT pg_reload_conf();` and repeat both.
  - **Predict:** WAL syncs per transaction for each run.
  - **Verify:** About 1 sync/txn at 1 client, well below 1 at 32 clients, and far fewer with `synchronous_commit = off`, alongside TPS. Reset `synchronous_commit` afterwards.
- [ ] **14.3 Crash, then time recovery** *(Level: Core)*
  - **Goal:** Connect §6 checkpoints to §15 recovery time.
  - **Do:** `ALTER SYSTEM SET max_wal_size = '8GB'; ALTER SYSTEM SET checkpoint_timeout = '30min';` then restart. Run pgbench for 5 minutes, note `docker compose exec -u postgres pg pg_controldata -D /var/lib/postgresql/data | grep REDO`, then `docker compose kill -s SIGKILL pg` and `docker compose start pg`. Read `docker compose logs pg | grep -E "redo (starts|done)"`. Repeat with `checkpoint_timeout = '1min'`.
  - **Predict:** Redo duration for each setting.
  - **Verify:** WAL distance between REDO point and crash, and the `elapsed:` time from the log, for both settings.
- [ ] **14.4 WAL compression** *(Level: Core)*
  - **Goal:** Measure §9 on an FPW-heavy workload.
  - **Do:** On a 5M-row table: `CHECKPOINT; SELECT pg_stat_reset_shared('wal');` then update 100k random rows. Read `wal_bytes, wal_fpi`. Repeat with `SET wal_compression = lz4;` and `= zstd;`.
  - **Predict:** WAL bytes saved by each.
  - **Verify:** A table of wal_bytes and update time for off, lz4 and zstd.
- [ ] **14.5 Build a torn-write-safe WAL** *(Level: Core)*
  - **Goal:** Retrieve §5 (ARIES) and §7 (CLRs) by building them.
  - **Do:** Python: append records `(lsn, txid, page_id, before, after, crc32)` to a file with `os.fsync` at commit. Recovery scans until the first bad CRC, redoes records whose LSN is above the page's pageLSN, and undoes losers while writing CLRs. Test harness: 1,000 runs, each truncating the log at a random byte and also crashing during undo.
  - **Predict:** Which invariant fails first if you drop the CRC check or the pageLSN check.
  - **Verify:** All 1,000 runs recover exactly the committed prefix. Compare with `WALManager` in [simpledb.py](simpledb.py) and [write-ahead-log-deep-dive.md](../solutions/write-ahead-log-deep-dive.md).
- [ ] **14.6 Break it: a forgotten replication slot** *(Level: Stretch)*
  - **Goal:** See §8 WAL retention go wrong and §12 logical decoding.
  - **Do:** `SELECT pg_create_logical_replication_slot('forgot', 'test_decoding');` Run pgbench for 5 minutes. Watch `SELECT slot_name, wal_status, pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) FROM pg_replication_slots;` and `du -sh /var/lib/postgresql/data/pg_wal` in the container. Peek at the changes with `SELECT * FROM pg_logical_slot_peek_changes('forgot', NULL, 5);`, then drop the slot.
  - **Predict:** pg_wal growth over 5 minutes.
  - **Verify:** Retained WAL grows past `max_wal_size` and shrinks after `pg_drop_replication_slot('forgot')` and a checkpoint. Name the guard setting (`max_slot_wal_keep_size`).

**Checkpoint (closed book):**
1. What do "steal" and "no-force" each require from the log?
2. Why does ARIES redo the losers' updates before undoing them?
3. Why is a CLR never undone?
<details><summary>Answers</summary>

1. Steal (dirty uncommitted pages may be flushed) requires undo information. No-force (commit without flushing data pages) requires redo information.
2. Repeating history restores the exact pre-crash state, including page LSNs and locks, so undo can run with normal logical rules against a known state.
3. A CLR records an undo that already happened and points (undoNextLSN) past the record it compensated; redo replays it, and undo skips over it, so work is never undone twice.
</details>

---

## 15 — SQL Performance Deep Dive  ([chapter](15-sql-performance-deep-dive.md))
**Time:** ~4 h · **Needs:** Postgres, pgbench

- [ ] **15.1 Triage with pg_stat_statements** *(Level: Core)*
  - **Goal:** Practice §2's "find it before you EXPLAIN it".
  - **Do:** `pgb -i -s 20 lab`. Write `labs/bench/bad.sql` containing `\set b random(1, 20)` and `SELECT count(*) FROM pgbench_accounts WHERE bid = :b;`. `SELECT pg_stat_statements_reset();` then `pgb -b select-only@9 -f /bench/bad.sql@1 -c 8 -T 60 lab`. Query the top 5 by `total_exec_time` with `calls, mean_exec_time, shared_blks_hit, shared_blks_read`.
  - **Predict:** The share of total time taken by a statement that is 10% of calls.
  - **Verify:** The ranked output. Add the fix, rerun, and record the before/after total time.
- [ ] **15.2 Anti-patterns, measured** *(Level: Core)*
  - **Goal:** Put buffers on three of §4's Deadly Dozen.
  - **Do:** (a) `users(email text)` with a B-tree on `email`, then `WHERE lower(email) = ...` before and after `CREATE INDEX ON users (lower(email))`. (b) `ORDER BY id OFFSET 100000 LIMIT 20` vs keyset `WHERE id > :last ORDER BY id LIMIT 20`. (c) `SELECT count(*) FROM a WHERE id NOT IN (SELECT ref FROM b)` after inserting one `NULL` into `b.ref`, vs `NOT EXISTS`.
  - **Predict:** Buffers for (a) and (b), and the row count for (c).
  - **Verify:** Buffers from `EXPLAIN (ANALYZE, BUFFERS)` for (a) and (b). For (c) the NOT IN count is 0 and NOT EXISTS gives the right answer.
- [ ] **15.3 Break it: stale statistics** *(Level: Core)*
  - **Goal:** Reproduce §3 "When statistics go wrong".
  - **Do:** `CREATE TABLE s (id int, k int) WITH (autovacuum_enabled = false);` Insert 10 rows, `ANALYZE s`, then insert 1M rows. `EXPLAIN ANALYZE` a join of `s` with `pgbench_accounts` on `k = aid`. Then `ANALYZE s` and rerun.
  - **Predict:** The estimated row count and the join algorithm before ANALYZE.
  - **Verify:** An estimate orders of magnitude off, a different join after ANALYZE, and both runtimes.
- [ ] **15.4 Break it: the lock queue pile-up** *(Level: Core)*
  - **Goal:** Reproduce §4 anti-pattern 10 and diagnose it with §7's tools.
  - **Do:** Session A: `BEGIN; SELECT count(*) FROM pgbench_accounts;` (leave open). Session B: `ALTER TABLE pgbench_accounts ADD COLUMN note text;` Session C: `SELECT * FROM pgbench_accounts WHERE aid = 1;` Session D: `SELECT pid, pg_blocking_pids(pid), wait_event_type, state, left(query, 60) FROM pg_stat_activity WHERE datname = 'lab';`
  - **Predict:** Whether C (a plain primary-key read) blocks.
  - **Verify:** C waits behind B, which waits behind A. Then redo B with `SET lock_timeout = '2s';` and show it failing fast while C runs.
- [ ] **15.5 Find the concurrency knee** *(Level: Stretch)*
  - **Goal:** Apply §10's load-testing method.
  - **Do:** `pgb -c N -j 4 -T 30 lab` for N in 1, 2, 4, 8, 16, 32, 64, 128. Record TPS and average latency.
  - **Predict:** The N where TPS stops rising.
  - **Verify:** A TPS/latency curve with the knee marked, and a Little's law check: TPS × latency ≈ N.

**Checkpoint (closed book):**
1. Why can a query with a high mean time matter less than one with a low mean time?
2. Why does `NOT IN` with a NULL in the subquery return no rows?
3. Why does an `ALTER TABLE` waiting for its lock block reads that don't conflict with the reader holding the lock?
<details><summary>Answers</summary>

1. Total impact is calls × mean. A 1 ms query called a million times outweighs a 2 s report run once an hour.
2. `x NOT IN (..., NULL)` is `x <> NULL AND ...`, which is NULL (not true) for every x.
3. Lock requests queue in order. The waiting ACCESS EXCLUSIVE request conflicts with everything, and later requests queue behind it rather than jumping ahead.
</details>

---

## 16 — Failure Detection and Leader Election  ([chapter](16-failure-detection-and-leader-election.md))
**Time:** ~4 h · **Needs:** Python, Docker (3-node etcd)

etcd cluster for this chapter (the image has no shell, so run `etcdctl` through `docker exec` on a node you are not killing):

```bash
docker network create etcdnet
for i in 1 2 3; do docker run -d --name etcd$i --network etcdnet quay.io/coreos/etcd:v3.5.17 etcd --name etcd$i \
  --initial-advertise-peer-urls http://etcd$i:2380 --listen-peer-urls http://0.0.0.0:2380 \
  --advertise-client-urls http://etcd$i:2379 --listen-client-urls http://0.0.0.0:2379 \
  --initial-cluster etcd1=http://etcd1:2380,etcd2=http://etcd2:2380,etcd3=http://etcd3:2380 \
  --initial-cluster-state new --heartbeat-interval 100 --election-timeout 1000; done
docker exec etcd1 etcdctl --endpoints=etcd1:2379,etcd2:2379,etcd3:2379 endpoint status -w table
```

- [ ] **16.1 Tune the phi-accrual detector** *(Level: Core)*
  - **Goal:** Measure §3.2's detection-time vs false-positive trade-off.
  - **Do:** Run [failure_detection_phi_accrual.py](failure_detection_phi_accrual.py) as is, then vary `phi_threshold` (1, 3, 8, 12) and the congestion multiplier (3× to 6×). Also run [failure_detection_push.py](failure_detection_push.py), [failure_detection_pull.py](failure_detection_pull.py) and [failure_detection_gossip.py](failure_detection_gossip.py) and count messages per round.
  - **Predict:** Detection delay after the crash at threshold 8, and the lowest threshold with no false positive during congestion.
  - **Verify:** A table of threshold × congestion → detection ticks and false positives, and messages/round for the other three detectors.
- [ ] **16.2 Time a Raft election** *(Level: Core)*
  - **Goal:** Measure §4.2 and §11.1 on etcd.
  - **Do:** Find the leader in the `endpoint status` table (`IS LEADER`). From another node, loop `etcdctl put k $(date +%s%N)` with `--command-timeout=300ms`, logging success/failure timestamps on the host. `docker kill` the leader. Recreate the cluster with `--election-timeout 5000` and repeat.
  - **Predict:** Write-unavailability window for each timeout.
  - **Verify:** The gap between the last success before the kill and the first success after it, for both settings (resolution about 100 ms from `docker exec`).
- [ ] **16.3 Break it: pause the leader like a GC stall** *(Level: Core)*
  - **Goal:** Watch §7 (check quorum, terms) and §10's pause scenarios.
  - **Do:** `docker pause <leader>` for 10 s while the write loop runs, then `docker unpause`. Record `RAFT TERM` and `IS LEADER` for all nodes before, during and after.
  - **Predict:** Whether writes continue during the pause, and what the old leader is when it wakes.
  - **Verify:** Writes resume after one election timeout, the term increases, and the old leader rejoins as a follower without accepting a stale write.
- [ ] **16.4 Leases need fencing** *(Level: Core)*
  - **Goal:** Reproduce §6.1's split-brain and §6.2's fix.
  - **Do:** Python simulation: a lock service grants 5 s leases with increasing tokens. Client A takes the lease, then "pauses" for 8 s (sleep without renewing). Client B takes the lease and writes. A wakes and writes. The storage first accepts all writes, then rejects any write whose token is lower than the highest it has seen.
  - **Predict:** Final storage value without and with fencing.
  - **Verify:** A's stale write wins without fencing and is rejected with fencing. On the real cluster, show that an etcd key's `create_revision` (`etcdctl get k -w json`) could serve as the token.
- [ ] **16.5 Split votes and randomized timeouts** *(Level: Stretch)*
  - **Goal:** Quantify why Raft randomizes (§4.2).
  - **Do:** Simulate 5 nodes that all lose the leader at t=0, with election timeouts drawn from [T, 1.05T], [T, 1.5T] and [T, 2T], and a 10 ms one-way message delay. Run 10,000 trials.
  - **Predict:** Mean election rounds for each range.
  - **Verify:** A table of mean and p99 time-to-leader and split-vote rate per range.

**Checkpoint (closed book):**
1. Why is a perfect failure detector impossible in an asynchronous system?
2. What does phi measure, and why does it adapt to network jitter?
3. Why does a lease alone not prevent two leaders from writing?
<details><summary>Answers</summary>

1. A crashed node and a slow node or network are indistinguishable by message timing; any timeout can be wrong (FLP).
2. The suspicion level −log10 of the probability that the next heartbeat is this late, given the recent inter-arrival distribution. Higher jitter widens the distribution, so the same delay yields lower phi.
3. The old holder can be paused (GC, VM stall) past expiry and resume believing it still holds the lease; only the resource checking a monotonic token can reject its writes.
</details>

---

## 17 — Latches and Locks Internals  ([chapter](17-latches-and-locks-internals.md))
**Time:** ~4 h · **Needs:** Go, gcc + strace (Linux), Postgres

- [ ] **17.1 Counter scaling ladder** *(Level: Core)*
  - **Goal:** Measure §5.2 cache-coherence cost.
  - **Do:** Go benchmarks for a shared counter incremented by G goroutines using `sync.Mutex`, `atomic.AddInt64`, and a sharded counter (one cache-line-padded slot per goroutine, summed on read). G = 1, 2, 4, …, 2 × `runtime.NumCPU()`.
  - **Predict:** Which variant gets slower per op as G grows, and which stays flat.
  - **Verify:** ns/op vs G for all three.
- [ ] **17.2 Spinlock collapse under oversubscription** *(Level: Core)*
  - **Goal:** See §8.1 and §8.5 in numbers.
  - **Do:** Implement a test-and-set spinlock (`CompareAndSwapInt32` loop), the same with `runtime.Gosched()` after N failed spins, and compare with `sync.Mutex`. Critical section: about 1 µs of work. Run with 4× more goroutines than `GOMAXPROCS`.
  - **Predict:** Throughput ranking.
  - **Verify:** Ops/s and p99 hold time for each lock. Explain the pure spinlock's result.
- [ ] **17.3 Watch the futex boundary** *(Level: Core)*
  - **Goal:** Verify §2.2 and §3.4: uncontended locks never enter the kernel.
  - **Do:** C: N threads each lock/increment/unlock a `pthread_mutex_t` 10M times. Build with `gcc -O2 -pthread`. Run `strace -f -c -e trace=futex ./m 1` and `./m 8`.
  - **Predict:** futex call count at 1 and 8 threads.
  - **Verify:** About 0 at 1 thread and a large count at 8.
- [ ] **17.4 Read Postgres wait events** *(Level: Core)*
  - **Goal:** Use §16.1 to tell heavyweight-lock waits from LWLock waits.
  - **Do:** Write `labs/bench/hot.sql` containing `UPDATE pgbench_branches SET bbalance = bbalance + 1 WHERE bid = 1;`. Run `pgb -f /bench/hot.sql -c 64 -j 8 -T 60 lab` while sampling `SELECT wait_event_type, wait_event, count(*) FROM pg_stat_activity WHERE state = 'active' GROUP BY 1,2 ORDER BY 3 DESC; \watch 1`. Then run `pgb -S -c 64 -j 8 -T 60 lab` and sample again.
  - **Predict:** The dominant wait event in each run.
  - **Verify:** `Lock / transactionid` or `Lock / tuple` for the hot row, and mostly CPU (null) or LWLock waits for the read-only run.
- [ ] **17.5 Optimistic reads with a version counter** *(Level: Stretch)*
  - **Goal:** Build §10's OLC idea.
  - **Do:** Go seqlock: writers take a mutex, bump `atomic.Uint64` version to odd, update two `atomic.Int64` fields keeping `a + b == 0`, then bump to even. Readers read the version, the fields, and the version again, and retry if it is odd or changed. Compare with `sync.RWMutex` at 1 writer / 15 readers.
  - **Predict:** Reader throughput ratio vs RWMutex.
  - **Verify:** Zero invariant violations over 10^8 reads, and reads/s for both designs.

**Checkpoint (closed book):**
1. List three differences between a latch and a lock.
2. Why does an MCS lock scale better than a test-and-set spinlock?
3. What problem does epoch-based reclamation solve?
<details><summary>Answers</summary>

1. Latches protect in-memory structures, locks protect logical database objects. Latches last microseconds, locks last for the transaction. Latches have no deadlock detection (avoided by ordering), locks have a lock manager with deadlock detection.
2. Each waiter spins on its own cache line, so a release invalidates only one waiter's line instead of every spinner's.
3. When memory unlinked from a lock-free structure can be freed: it is freed only after every thread that might still hold a pointer has left the epoch in which it was unlinked.
</details>

---

## 18 — Concurrency Control and Scheduling  ([chapter](18-concurrency-control-and-scheduling.md))
**Time:** ~4 h · **Needs:** Python, Postgres (two or three psql sessions)

- [ ] **18.1 A conflict-serializability checker** *(Level: Core)*
  - **Goal:** Retrieve §1.3–1.5.
  - **Do:** Python: parse schedules like `r1(x) w2(x) r2(y) w1(y) c1 c2`, build the precedence graph, and detect cycles. Run it on the chapter's §1.5 examples, then on 10,000 random schedules of 3 transactions × 3 operations over 2 items.
  - **Predict:** The fraction of random schedules that are conflict-serializable.
  - **Verify:** The checker agrees with every chapter example, plus the measured fraction.
- [ ] **18.2 Snapshot visibility by hand** *(Level: Core)*
  - **Goal:** Apply §4.2's snapshot rules to real xids.
  - **Do:** Session A: `BEGIN ISOLATION LEVEL REPEATABLE READ; SELECT pg_current_snapshot();` Sessions B and C insert rows; B commits before A's snapshot, C starts before and commits after it, D starts after. In A: `SELECT xmin, * FROM t;` Then check `SELECT xmin, pg_visible_in_snapshot(xmin::text::xid8, pg_current_snapshot()) FROM t;` from A.
  - **Predict:** From `xmin:xmax:xip_list` alone, which rows A can see.
  - **Verify:** Your predictions match `pg_visible_in_snapshot` for every row.
- [ ] **18.3 Make a deadlock and watch it get caught** *(Level: Core)*
  - **Goal:** See §6.3's wait-for graph in Postgres.
  - **Do:** Session A: `BEGIN; UPDATE acct SET bal = bal - 1 WHERE id = 1;` Session B: `BEGIN; UPDATE acct SET bal = bal - 1 WHERE id = 2; UPDATE acct SET bal = bal + 1 WHERE id = 1;` Session C: `SELECT pid, pg_blocking_pids(pid), wait_event FROM pg_stat_activity WHERE wait_event_type = 'Lock';` Then in A: `UPDATE acct SET bal = bal + 1 WHERE id = 2;` Repeat with `SET deadlock_timeout = '5s';` in both sessions.
  - **Predict:** Which session is aborted and how long detection takes.
  - **Verify:** `ERROR: deadlock detected` after about `deadlock_timeout`, and the cycle in `pg_blocking_pids` before it fires.
- [ ] **18.4 Wait-die vs wound-wait** *(Level: Core)*
  - **Goal:** Compare §6.2's prevention schemes with detection.
  - **Do:** Python discrete-event simulation: strict 2PL, 50 concurrent transactions each locking 5 of K items (K = 20, 100, 1000). Policies: wait-die, wound-wait, and detection by cycle search.
  - **Predict:** Which policy has the most aborts at K = 20.
  - **Verify:** Aborts per commit and throughput for each policy × K.
- [ ] **18.5 Optimistic vs pessimistic under contention** *(Level: Core)*
  - **Goal:** Find §5.3's crossover on real Postgres.
  - **Do:** 16 Python workers each run 1,000 transfers over H hot rows (H = 1, 10, 100, 10,000). Variant 1: `SELECT ... FOR UPDATE` then update. Variant 2: read `version`, then `UPDATE ... SET ..., version = version + 1 WHERE id = %s AND version = %s`, retrying when 0 rows are updated.
  - **Predict:** The H where OCC starts to win.
  - **Verify:** Transfers/s and retries per commit for both variants at each H.
- [ ] **18.6 SIRead lock granularity** *(Level: Stretch)*
  - **Goal:** Observe §3.5's SSI predicate locks.
  - **Do:** At SERIALIZABLE, run (a) a seq scan, (b) a primary-key lookup of 1 row, (c) an index range read of 3 rows from one page, and before each COMMIT list `SELECT locktype, relation::regclass, page, tuple FROM pg_locks WHERE mode = 'SIReadLock';`
  - **Predict:** The lock granularity for (a), (b) and (c) given `max_pred_locks_per_page = 2`.
  - **Verify:** (a) one relation lock; (b) a heap tuple lock plus a page lock on the index leaf; (c) the three tuple locks promoted to one heap page lock. Explain why coarse locks cause false-positive serialization failures.

**Checkpoint (closed book):**
1. What makes two operations conflict?
2. Why is strict 2PL preferred over basic 2PL?
3. In wound-wait, what happens when an older transaction requests a lock held by a younger one?
<details><summary>Answers</summary>

1. They belong to different transactions, touch the same item, and at least one is a write.
2. Holding write locks until commit makes schedules strict: no transaction reads uncommitted data, so there are no cascading aborts.
3. The older one "wounds" (aborts) the younger holder and takes the lock; a younger requester would wait instead.
</details>

---

## 19 — Distributed Databases Deep Dive  ([chapter](19-distributed-databases-deep-dive.md))
**Time:** ~4 h · **Needs:** Python, Postgres (`max_prepared_transactions` is set in the compose file)

- [ ] **19.1 Hybrid logical clocks** *(Level: Core)*
  - **Goal:** Implement §3.4 and test its guarantee.
  - **Do:** Python HLC with `send()`, `recv(remote)` and `(l, c)` timestamps. Simulate 3 nodes with physical clock offsets of −50, 0 and +80 ms and random message delays. Log every send/receive pair.
  - **Predict:** How many causally ordered pairs get inverted timestamps using raw physical time vs HLC.
  - **Verify:** Many inversions with physical time and 0 with HLC. Also report max `l − physical` and max `c`.
- [ ] **19.2 Break it: 2PC blocks when the coordinator dies** *(Level: Core)*
  - **Goal:** Watch §4.1's blocking problem in a real engine.
  - **Do:** Create databases `lab` and `lab2`, each with `acct(id int primary key, bal int)`. A Python coordinator runs an update in both, then `PREPARE TRANSACTION 'tx1'` on both, then exits before `COMMIT PREPARED`. Check `SELECT * FROM pg_prepared_xacts;`, try `UPDATE acct ... WHERE id = <same row>` from psql, and restart Postgres.
  - **Predict:** Whether the prepared transaction survives a restart, and whether the other update blocks.
  - **Verify:** The update blocks, the prepared transaction survives the restart, and `COMMIT PREPARED 'tx1'` (or `ROLLBACK PREPARED`) by hand releases it. Write down who is allowed to decide.
- [ ] **19.3 CRDTs under chaos** *(Level: Core)*
  - **Goal:** Test §7.3's convergence claims.
  - **Do:** Implement G-Counter, PN-Counter and an OR-Set. Fuzz: 3 replicas, 10,000 random ops, deliver merges in random order with duplicates. Then do the same with a last-write-wins register using skewed clocks.
  - **Predict:** Whether each type converges, and how many concurrent writes LWW silently drops.
  - **Verify:** All CRDT replicas equal after the final merge in every trial, and the count of lost LWW writes.
- [ ] **19.4 Merkle-tree anti-entropy** *(Level: Core)*
  - **Goal:** Measure §8.1's bandwidth savings.
  - **Do:** Two replicas of 1M keys, differing in k keys (k = 1, 100, 10,000). Build Merkle trees with fanout 16 over key-hash ranges and walk down only mismatched branches.
  - **Predict:** Hashes exchanged for each k.
  - **Verify:** Hashes exchanged vs k, compared with the 1M needed for a full comparison.
- [ ] **19.5 Ranges and node loss in CockroachDB** *(Level: Stretch, optional)*
  - **Goal:** See §6.5 per-range consensus on a real system.
  - **Do:** `docker run -it --rm cockroachdb/cockroach:latest demo --nodes=3`. Create a table, `ALTER TABLE t SPLIT AT VALUES (1000), (2000);`, `SHOW RANGES FROM TABLE t;`. Then `\demo shutdown 3` and keep reading and writing.
  - **Predict:** Whether reads and writes keep working with one of three nodes down.
  - **Verify:** Queries succeed, and range output before and after shows replicas and leaseholders moving.

**Checkpoint (closed book):**
1. What does HLC guarantee that physical clocks don't, and what does it still not give you?
2. Why can a 2PC participant not decide alone after voting yes?
3. Why must a CRDT merge be commutative, associative and idempotent?
<details><summary>Answers</summary>

1. If a happened before b, then hlc(a) < hlc(b), while staying close to physical time. It does not give real-time ordering of causally unrelated events (no external consistency without bounded clock error like TrueTime).
2. It promised to commit if told to, and the coordinator may already have told others to commit; aborting alone could split the outcome.
3. Replicas receive merges in any order, grouping and number of times; those three properties make the final state independent of all of them.
</details>

---

## 20 — Time-Series Database in Go  ([chapter](20-time-series-database-golang.md))
**Time:** ~5 h · **Needs:** Go

Also do §11 (build MiniTSDB). The tasks below measure and break what you built.

- [ ] **20.1 Bytes per sample** *(Level: Core)*
  - **Goal:** Verify §5's Gorilla compression claim.
  - **Do:** Encode 120-sample chunks for three series: regular 15 s timestamps with a slowly changing gauge; the same with ±500 ms jitter; and random float64 values.
  - **Predict:** Bytes per sample for each.
  - **Verify:** Close to §5.3's figure for the first series, and a clear rise for jitter and random values. Explain which bits grow in each case.
- [ ] **20.2 Break it: cardinality explosion** *(Level: Core)*
  - **Goal:** See §6 and §15's high-cardinality pitfall.
  - **Do:** Ingest 1,000 series, then add a `request_id` label that is unique per sample, for 1M samples. Measure `runtime.ReadMemStats` HeapAlloc, series count, and postings-list sizes.
  - **Predict:** Heap growth and bytes per series.
  - **Verify:** Heap and series count before and after, and query latency for `{job="api"}` before and after.
- [ ] **20.3 Crash and replay the WAL** *(Level: Core)*
  - **Goal:** Test §7.1's durability claim against your code.
  - **Do:** Ingest in a loop that prints the last acknowledged sample, `kill -9` the process, restart, and count recovered samples. Then batch WAL fsyncs every 100 ms and repeat.
  - **Predict:** Samples lost in each mode.
  - **Verify:** Zero acknowledged samples lost with per-write fsync. For batched fsync, samples lost stay within the window, and ingest rate rises.
- [ ] **20.4 Posting-list intersection** *(Level: Core)*
  - **Goal:** Measure §6.1.
  - **Do:** Benchmark intersection of sorted `[]uint64` lists of 1M and 10k entries: linear merge, galloping (exponential search), and map lookup.
  - **Predict:** The fastest method for the skewed pair.
  - **Verify:** `go test -bench` results for all three, plus a balanced 1M × 1M pair.
- [ ] **20.5 Race-free head block** *(Level: Stretch)*
  - **Goal:** Apply §12's concurrency patterns.
  - **Do:** Run concurrent appenders and range queries under `go test -race`. Fix any races, then replace a single mutex with sharded mutexes (§12.1).
  - **Predict:** Append throughput gain from sharding at 8 writers.
  - **Verify:** A clean `-race` run and appends/s before and after sharding.

**Checkpoint (closed book):**
1. Why does delta-of-delta encoding make regular timestamps nearly free?
2. Why are labels an inverted index rather than table columns?
3. What does the head block hold, and why does it need a WAL?
<details><summary>Answers</summary>

1. Regular intervals make the second difference 0, which Gorilla encodes in one bit.
2. Queries select series by arbitrary label matchers; posting lists (label=value → series IDs) turn that into sorted-list intersections, and new labels need no schema change.
3. The most recent samples in memory (open chunks, about the last 2 hours). Without a WAL a crash loses everything not yet cut into a persistent block.
</details>

---

## 21 — In-Process OLAP: DuckDB and chDB  ([chapter](21-in-process-olap-duckdb-chdb.md))
**Time:** ~3 h · **Needs:** Python + duckdb, chdb, polars, pyarrow

- [ ] **21.1 Bigger than RAM** *(Level: Core)*
  - **Goal:** Watch §7's out-of-core execution.
  - **Do:** `con = duckdb.connect('ooc.duckdb')`, `CREATE TABLE big AS SELECT range AS id, hash(range) % 50000000 AS k, random() AS v FROM range(100000000);` Then `SET temp_directory = 'spill';` and run `SELECT k, sum(v) FROM big GROUP BY k ORDER BY 2 DESC LIMIT 10;` with `memory_limit` 8GB, 2GB and 512MB. Watch `du -sh spill` in another terminal.
  - **Predict:** Slowdown at each limit, and peak spill size.
  - **Verify:** Time and peak spill per limit. **Break it:** add `SET max_temp_directory_size = '200MB';` at 512MB and record the error.
- [ ] **21.2 Zone maps need sorted data** *(Level: Core)*
  - **Goal:** Measure §3's zone maps and §12 anti-pattern 4.
  - **Do:** Two copies of a 100M-row events table, one `ORDER BY ts`, one `ORDER BY random()`. Time `SELECT count(*) FROM t WHERE ts BETWEEN <one hour>` on each and compare `EXPLAIN ANALYZE`.
  - **Predict:** The speedup on the sorted copy.
  - **Verify:** Both times and the rows scanned by each table scan.
- [ ] **21.3 Zero-copy Arrow** *(Level: Core)*
  - **Goal:** Verify §8's zero-copy claim.
  - **Do:** Build a 50M-row `pyarrow` table. Measure time and RSS growth (`resource.getrusage(resource.RUSAGE_SELF).ru_maxrss`) for `duckdb.sql("SELECT sum(x) FROM tbl")`, then for `tbl.to_pandas()`, then for `CREATE TABLE copy AS SELECT * FROM tbl`.
  - **Predict:** RSS growth for each.
  - **Verify:** Near-zero growth for the query and growth close to the table size for the conversions.
- [ ] **21.4 Break it: two writers, one file** *(Level: Core)*
  - **Goal:** See §9's concurrency model.
  - **Do:** Process 1 opens `ooc.duckdb` read-write and sleeps. Process 2 opens it read-write, then with `read_only=True`. Then close process 1 and open the file read-only from two processes at once.
  - **Predict:** Which opens succeed.
  - **Verify:** The `Could not set lock on file` error text, and the combinations that work.
- [ ] **21.5 Same Parquet, three engines** *(Level: Stretch)*
  - **Goal:** Ground §11's comparison in your own numbers.
  - **Do:** Write TPC-H `lineitem` (sf=1, from 08.1) to Parquet. Run a Q1-style aggregation with DuckDB, chDB (`chdb.query("SELECT ... FROM file('lineitem.parquet')", "PrettyCompact")`) and Polars (`pl.scan_parquet(...).group_by(...).agg(...).collect()`), 3 warm runs each.
  - **Predict:** The ranking.
  - **Verify:** Median time and peak RSS per engine.

**Checkpoint (closed book):**
1. What is a pipeline breaker, and which operators are one?
2. Why is DuckDB not a good fit for a multi-writer OLTP service?
3. What does morsel-driven parallelism fix compared with static partitioning?
<details><summary>Answers</summary>

1. An operator that must consume all its input before producing output: hash-join build, full sort, hash aggregation.
2. One process holds the write lock on the file, and its MVCC and storage are optimized for bulk scans and appends, not many small concurrent transactions.
3. Load imbalance: workers grab small chunks dynamically, so a slow or skewed partition doesn't leave other cores idle.
</details>

---

## 22 — Data Lake and Lakehouse  ([chapter](22-data-lake-lakehouse.md))
**Time:** ~4 h · **Needs:** Python + pyarrow, duckdb, deltalake, pyiceberg

- [ ] **22.1 Read a Parquet footer by hand** *(Level: Core)*
  - **Goal:** Verify §2's physical layout.
  - **Do:** Write 10M rows with DuckDB `COPY (...) TO 'ev.parquet' (FORMAT parquet, ROW_GROUP_SIZE 100000)`. In Python: `f.seek(-8, 2); tail = f.read(8)`, parse the 4-byte little-endian footer length, and check the magic bytes `PAR1`. Compare with `SELECT * FROM parquet_metadata('ev.parquet')`.
  - **Predict:** Footer size and row-group count.
  - **Verify:** Your parsed footer length and the row-group count from `parquet_metadata`. Explain why the footer is at the end (§2).
- [ ] **22.2 Row-group pruning** *(Level: Core)*
  - **Goal:** Measure §2 row-group sizing and §14 min/max pruning.
  - **Do:** Write the same data sorted by `ts` and shuffled, each at row-group sizes 10k, 100k and 1M. For each file, count row groups whose `stats_min`/`stats_max` for `ts` overlap a one-hour filter (from `parquet_metadata`), and time the filtered query.
  - **Predict:** Row groups read for each of the 6 files.
  - **Verify:** A 2×3 table of prunable row groups and query time.
- [ ] **22.3 Delta log, time travel, and a vacuum that breaks it** *(Level: Core)*
  - **Goal:** Take apart §5's transaction log.
  - **Do:** `write_deltalake('dt', tbl, mode='append')` 20 times. Read `_delta_log/*.json` and count `add` actions. `DeltaTable('dt', version=3).to_pyarrow_table()`. Then `dt.optimize.compact()` and `dt.vacuum(retention_hours=0, enforce_retention_duration=False, dry_run=False)`, and time-travel to version 3 again.
  - **Predict:** Files before and after compaction, and whether version 3 is still readable after vacuum.
  - **Verify:** File counts from `dt.file_uris()`, the compaction metrics, and a `FileNotFoundError` for version 3 after vacuum.
- [ ] **22.4 Walk Iceberg's metadata tree** *(Level: Core)*
  - **Goal:** Map §4's metadata hierarchy to files.
  - **Do:** `SqlCatalog("lab", uri="sqlite:///wh/cat.db", warehouse="file://<abs path>/wh")`. Create a table, append 3 times, and evolve the schema (`with t.update_schema() as u: u.add_column("note", StringType())`). List `wh/<ns>/<table>/metadata/`, then read `t.inspect.snapshots()`, `t.inspect.manifests()` and `t.inspect.files()`. Scan an old snapshot with `t.scan(snapshot_id=...)`.
  - **Predict:** Number of `metadata.json`, manifest-list (`snap-*.avro`) and manifest files after these 5 operations.
  - **Verify:** Your counts match the directory listing. The old snapshot reads without the new column.
- [ ] **22.5 The small-files problem** *(Level: Core)*
  - **Goal:** Quantify §11 and §17.
  - **Do:** Write 5,000 Parquet files of 1,000 rows each, and one compacted file with the same rows. Time `SELECT count(*), sum(v) FROM 'small/*.parquet'` against the compacted file. Then compute the S3 GET cost of one full scan of each layout at $0.0004 per 1,000 GETs, assuming at least 2 GETs per file (footer + data).
  - **Predict:** The time ratio.
  - **Verify:** Both times, and the GET count and cost per scan for each layout.
- [ ] **22.6 DuckLake: the catalog is a database** *(Level: Stretch)*
  - **Goal:** Compare §8's DuckLake design with 22.3–22.4.
  - **Do:** `INSTALL ducklake; ATTACH 'ducklake:meta.ducklake' AS lake (DATA_PATH 'lake_files/');` Create a table, insert 5 times, update rows, and query the metadata tables in `meta.ducklake`.
  - **Predict:** Files created per insert compared with Delta.
  - **Verify:** The file listing, and where snapshot information lives (SQL rows, not JSON/Avro files).

**Checkpoint (closed book):**
1. What does an open table format add on top of a folder of Parquet files?
2. Why does Delta Lake write periodic checkpoint files?
3. What is hidden partitioning in Iceberg, and what problem does it remove?
<details><summary>Answers</summary>

1. An atomic, versioned list of which files make up the table (snapshots), which gives ACID commits, time travel, schema evolution and file-level statistics for pruning.
2. So readers don't replay thousands of JSON commits: a Parquet checkpoint summarizes table state up to a version.
3. Partitions derive from column transforms (e.g. `day(ts)`) stored in metadata, so queries filter on `ts` and still prune; users can't get the partition column wrong, and the scheme can evolve.
</details>

---

## Capstone projects

- [ ] **C1 — A crash-safe key-value engine** (2–3 days; chapters 00, 01, 06 or 13, 14)
  - **Spec:** A single-node KV store with `put/get/delete/scan`, built as either a B+tree over a buffer pool or an LSM (memtable, SSTables, compaction, Bloom filters). Every write goes through a CRC-checked WAL with group commit. Recovery replays the WAL. Start from your 14.5 code and the structure of [simpledb.py](simpledb.py); [key-value-store-design.md](../solutions/key-value-store-design.md) shows the distributed version.
  - **Acceptance:** A harness runs 200 cycles of "write with random keys, `kill -9` at a random moment, restart, verify". No acknowledged write is lost and no unacknowledged write appears half-applied. `scan` returns keys in order after every recovery.
  - **Measure:** Write, read and space amplification. fsyncs per committed put at 1 and 32 client threads. p50/p99 put latency. Recovery time per GB of WAL.
- [ ] **C2 — Postgres performance forensics** (1–2 days; chapters 03, 04, 05, 06, 07, 15, 17)
  - **Spec:** A partner (or a script you write then forget for a week) plants 5 faults in a pgbench-based app from this list: missing index, stale stats, bloated table with autovacuum off, long idle transaction, lock-queue pile-up, generic-plan trap, `work_mem` spill, N+1 query loop. You get only the app and the database.
  - **Acceptance:** Each fault is identified with evidence (a `pg_stat_statements` row, a plan, a `pg_locks` or `pg_stat_activity` row) before you change anything, then fixed. You deliver a one-page postmortem.
  - **Measure:** p50/p95 latency and TPS before and after each fix, and total time-to-diagnosis per fault.
- [ ] **C3 — Replicated KV with leases, fencing and a linearizability check** (2–3 days; chapters 12, 14, 16, 19)
  - **Spec:** Three replicas of your C1 engine (or a dict with a log). The leader holds an etcd lease (from §16's cluster) and ships log entries to followers, and commits after a majority ack. Every write carries the lease's fencing token, and followers reject stale tokens. Clients record an operation history (invoke/ok/fail with timestamps).
  - **Acceptance:** Under `kill -9`, `docker pause` of the leader and `tc qdisc add dev eth0 root netem delay 200ms loss 20%`, the history passes a linearizability checker ([Porcupine](https://github.com/anishathalye/porcupine) in Go, or your own for a single register). Turning off fencing or serving reads from followers must make the checker fail.
  - **Measure:** Write latency p50/p99, unavailability window per failover, and the number of violations found with each safety mechanism removed.
- [ ] **C4 — CDC to a mini lakehouse** (2 days; chapters 08, 09, 14, 21, 22)
  - **Spec:** pgbench runs on Postgres. A Python loop reads changes from a logical slot (`pg_logical_slot_get_changes` with `test_decoding`, or `pgoutput` via psycopg replication), appends them to a bronze Delta table, merges them into a silver table (`dt.merge(...).when_matched_update_all().when_not_matched_insert_all().execute()`), and builds gold aggregates with DuckDB. A compaction job runs every 10 minutes.
  - **Acceptance:** A gold query (balance per branch) matches the same query on Postgres at a chosen LSN. A restart of the CDC loop loses and duplicates nothing (confirmed slot position + idempotent merge). The replication slot never retains more than 1 GB of WAL.
  - **Measure:** Freshness lag p50/p99 (commit time to gold visibility), files per table before and after compaction, gold query latency, and retained WAL over a 1-hour run.
