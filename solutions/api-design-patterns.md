# API Design Patterns & Anti-Patterns

A comprehensive guide based on "Patterns for API Design: Simplifying Integration with Loosely Coupled Message Exchanges" by Olaf Zimmermann, Mirko Stocker, Daniel Lübke, Uwe Zdun, and Cesare Pautasso, combined with Big Tech industry standards from Google, Microsoft, AWS, Stripe, and others.

---

## Table of Contents

1. [Foundation Patterns](#1-foundation-patterns)
2. [Responsibility Patterns](#2-responsibility-patterns)
3. [Structure Patterns](#3-structure-patterns)
4. [Quality Patterns](#4-quality-patterns)
5. [Evolution Patterns](#5-evolution-patterns)
6. [Big Tech API Standards](#6-big-tech-api-standards)
7. [Anti-Patterns](#7-anti-patterns)
8. [Best Practices Checklist](#8-best-practices-checklist)

---

## 1. Foundation Patterns

### 1.1 API Description

**Intent:** Provide machine-readable and human-readable documentation of API capabilities.

**Problem:** How can API providers describe the capabilities of their APIs so that clients can discover and use them?

**Solution:** Create comprehensive API descriptions using:
- OpenAPI/Swagger for REST APIs
- Protocol Buffers for gRPC
- GraphQL SDL for GraphQL APIs
- AsyncAPI for event-driven APIs

```yaml
# OpenAPI Example
openapi: 3.0.3
info:
  title: User Service API
  version: 1.0.0
  description: |
    Service for managing user accounts.

    ## Rate Limits
    - 1000 requests/minute for authenticated users
    - 100 requests/minute for anonymous users
paths:
  /users/{id}:
    get:
      operationId: getUser
      summary: Retrieve a user by ID
      parameters:
        - name: id
          in: path
          required: true
          schema:
            type: string
            format: uuid
      responses:
        '200':
          description: User found
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/User'
```

**Big Tech Example (Google):**
```protobuf
// Google uses Protocol Buffers with extensive comments
service LibraryService {
  // Gets a book from the library.
  // Returns NOT_FOUND if the book does not exist.
  rpc GetBook(GetBookRequest) returns (Book) {
    option (google.api.http) = {
      get: "/v1/{name=publishers/*/books/*}"
    };
  }
}
```

---

### 1.2 Version Identifier

**Intent:** Communicate API version to enable evolution without breaking clients.

**Problem:** How can APIs evolve while maintaining backward compatibility?

**Solution:** Include explicit version identifiers in API contracts.

**Versioning Strategies:**

| Strategy | Example | Pros | Cons |
|----------|---------|------|------|
| URL Path | `/v1/users` | Clear, cacheable | URL pollution |
| Query Parameter | `/users?version=1` | Flexible | Easy to forget |
| Header | `API-Version: 1` | Clean URLs | Less visible |
| Content Negotiation | `Accept: application/vnd.api+json;version=1` | RESTful | Complex |

**Google Standard:**
```
// URL-based versioning with major version only
GET https://library.googleapis.com/v1/publishers/123/books

// Version in resource name for fine-grained control
name: "projects/my-project/locations/us-central1/datasets/my-dataset"
```

**Stripe Standard:**
```bash
# Header-based with dated versions
curl https://api.stripe.com/v1/charges \
  -H "Stripe-Version: 2023-10-16"
```

**Microsoft Standard:**
```
# Query parameter approach
GET https://graph.microsoft.com/v1.0/users?api-version=2023-01-01
```

---

### 1.3 Semantic Versioning for APIs

**Pattern:** Use MAJOR.MINOR.PATCH versioning:
- **MAJOR:** Breaking changes
- **MINOR:** Backward-compatible additions
- **PATCH:** Backward-compatible fixes

```json
{
  "api_version": "2.3.1",
  "deprecation_notice": {
    "deprecated_fields": ["legacy_id"],
    "sunset_date": "2024-06-01",
    "migration_guide": "https://docs.example.com/migration/v3"
  }
}
```

---

## 2. Responsibility Patterns

### 2.1 Processing Resource

**Intent:** Expose API endpoints that perform computations or trigger actions.

**Problem:** How can APIs expose processing capabilities that go beyond CRUD operations?

**Solution:** Create resources that represent actions or computations.

```http
# Anti-pattern: Verb in URL
POST /api/calculateTax

# Pattern: Resource representing the computation
POST /api/tax-calculations
Content-Type: application/json

{
  "items": [...],
  "shipping_address": {...}
}

# Response includes computation result
{
  "id": "calc_abc123",
  "total_tax": 45.67,
  "breakdown": [...]
}
```

**Google Standard - Custom Methods:**
```http
# Use colon for custom actions
POST /v1/projects/123/instances/456:start
POST /v1/users/123:undelete
POST /v1/documents/456:batchGet
```

---

### 2.2 Information Holder Resource

**Intent:** Expose data entities through standard CRUD operations.

**Categories:**

| Type | Description | Example |
|------|-------------|---------|
| **Master Data Holder** | Core business entities | `/customers`, `/products` |
| **Operational Data Holder** | Transactional data | `/orders`, `/payments` |
| **Reference Data Holder** | Lookup/config data | `/countries`, `/currencies` |

```http
# Master Data - Customer
GET /api/v1/customers/cust_123
{
  "id": "cust_123",
  "type": "master_data",
  "name": "Acme Corp",
  "created_at": "2023-01-15T10:30:00Z",
  "updated_at": "2023-06-20T14:22:00Z"
}

# Operational Data - Order
GET /api/v1/orders/ord_456
{
  "id": "ord_456",
  "type": "operational_data",
  "customer_id": "cust_123",
  "status": "processing",
  "items": [...],
  "created_at": "2023-06-25T09:00:00Z"
}

# Reference Data - Country
GET /api/v1/reference/countries/US
{
  "code": "US",
  "type": "reference_data",
  "name": "United States",
  "currency": "USD",
  "cacheable_until": "2024-01-01T00:00:00Z"
}
```

---

### 2.3 Link Lookup Resource (HATEOAS)

**Intent:** Provide hypermedia links to guide clients through API capabilities.

**Problem:** How can clients discover related resources and available actions?

**Solution:** Include links in responses that describe possible transitions.

```json
{
  "id": "order_123",
  "status": "pending_payment",
  "total": 99.99,
  "_links": {
    "self": {
      "href": "/api/v1/orders/order_123"
    },
    "customer": {
      "href": "/api/v1/customers/cust_456"
    },
    "pay": {
      "href": "/api/v1/orders/order_123/payments",
      "method": "POST",
      "title": "Submit payment for this order"
    },
    "cancel": {
      "href": "/api/v1/orders/order_123",
      "method": "DELETE",
      "title": "Cancel this order"
    }
  },
  "_embedded": {
    "items": [...]
  }
}
```

**GitHub API Example:**
```json
{
  "id": 1,
  "name": "octocat/Hello-World",
  "url": "https://api.github.com/repos/octocat/Hello-World",
  "html_url": "https://github.com/octocat/Hello-World",
  "pulls_url": "https://api.github.com/repos/octocat/Hello-World/pulls{/number}",
  "issues_url": "https://api.github.com/repos/octocat/Hello-World/issues{/number}"
}
```

---

## 3. Structure Patterns

### 3.1 Atomic Parameter

**Intent:** Use simple, self-contained values as parameters.

**Problem:** How should simple data be represented in API requests and responses?

**Solution:** Use primitive types with clear semantics.

```json
{
  "user_id": "usr_abc123",
  "email": "user@example.com",
  "age": 28,
  "is_active": true,
  "balance": 1250.50,
  "created_at": "2023-06-15T10:30:00Z"
}
```

**Type Guidelines:**

| Data Type | Format | Example |
|-----------|--------|---------|
| Identifiers | String (prefixed) | `"usr_abc123"`, `"ord_xyz789"` |
| Timestamps | ISO 8601 | `"2023-06-15T10:30:00Z"` |
| Money | Object with amount + currency | `{"amount": 1000, "currency": "USD"}` |
| Duration | ISO 8601 duration | `"P1DT2H30M"` |
| Enums | Uppercase snake_case | `"PENDING"`, `"IN_PROGRESS"` |

---

### 3.2 Parameter Tree (Nested Structures)

**Intent:** Organize related parameters into hierarchical structures.

**Problem:** How should complex, nested data be represented?

**Solution:** Use nested objects with clear hierarchy.

```json
{
  "order": {
    "id": "ord_123",
    "customer": {
      "id": "cust_456",
      "name": "John Doe",
      "shipping_address": {
        "line1": "123 Main St",
        "line2": "Apt 4B",
        "city": "San Francisco",
        "state": "CA",
        "postal_code": "94102",
        "country": "US"
      }
    },
    "items": [
      {
        "product_id": "prod_789",
        "quantity": 2,
        "unit_price": {
          "amount": 2999,
          "currency": "USD"
        }
      }
    ]
  }
}
```

**Flattening for Query Parameters:**
```http
# Nested structure in query params using dot notation
GET /api/search?filter.status=active&filter.created_at.gte=2023-01-01&sort.field=name&sort.order=asc
```

---

### 3.3 Parameter Forest (Collections)

**Intent:** Handle collections of items consistently.

**Solution:** Use arrays with consistent item structure.

```json
{
  "data": [
    {"id": "1", "name": "Item 1"},
    {"id": "2", "name": "Item 2"}
  ],
  "meta": {
    "total_count": 150,
    "page": 1,
    "per_page": 20
  }
}
```

---

### 3.4 Pagination Patterns

**Intent:** Return large collections in bounded pages. Each page should cost the same, and no rows should be skipped or repeated while the data changes underneath.

Your pagination contract decides which SQL you can run. That makes it a database-performance decision more than a JSON-shape decision. Once clients depend on `?page=4817`, you are committed to `OFFSET` for the life of that API version.

#### 3.4.1 The three strategies at a glance

| | Offset / page number | Keyset (seek) | Opaque cursor / page token |
|---|---|---|---|
| Request | `?offset=200&limit=20` or `?page=11` | `?after_created_at=…&after_id=…` | `?page_token=eyJ2Ijox…` |
| SQL | `ORDER BY … LIMIT n OFFSET k` | `WHERE (a, id) < ($a, $id) ORDER BY a, id LIMIT n` | Keyset underneath; the token encodes `($a, $id)` |
| Cost of page *p* | **O(offset)**: grows linearly | **O(page size)**: constant | Constant |
| Jump to page 500 | Yes | No | No |
| Stable under inserts/deletes | **No**: rows are skipped or duplicated | Yes | Yes |
| Client can tamper / couples to schema | Low risk | **Yes**: column names and values are public | No, if signed |
| Used by | Admin UIs, small bounded lists | Internal APIs, log tailing | Stripe, Google AIP-158, Slack, GitHub GraphQL, Relay |

**Default for public APIs:** opaque cursor (keyset inside). Offer offset only for small, bounded collections (for example, below 10k rows) where users need numbered pages.

#### 3.4.2 Measured: why OFFSET gets slower with depth

`OFFSET k` doesn't skip rows; it **produces and discards** `k` rows. Even with a perfect index, the executor walks the index from the start, fetches each heap tuple (to check visibility under MVCC), and throws it away. The work is `O(k + n)`.

Benchmark: PostgreSQL 16, 1M-row `orders` table (135 MB heap), B-tree on `(created_at DESC, id DESC)`, warm cache, 4 vCPU, median of 7 runs of `EXPLAIN (ANALYZE, BUFFERS)`:

```sql
-- offset
SELECT * FROM orders ORDER BY created_at DESC, id DESC LIMIT 10 OFFSET :k;
-- keyset at the same position (cursor = last row of the previous page)
SELECT * FROM orders WHERE (created_at, id) < (:last_created_at, :last_id)
ORDER BY created_at DESC, id DESC LIMIT 10;
```

| Query | Rows the index scan actually read | Buffers touched | Time |
|---|---:|---:|---:|
| `LIMIT 10` (page 1) | 10 | 4 | 0.05 ms |
| `LIMIT 10 OFFSET 1000` | 1,010 | 24 | 0.2 ms |
| `LIMIT 10 OFFSET 10000` | 10,010 | 214 | 1.9 ms |
| `LIMIT 10 OFFSET 100000` | 100,010 | 2,111 | 17 ms |
| `LIMIT 10 OFFSET 500000` | 500,010 | 10,539 | 80 ms |
| `LIMIT 10 OFFSET 900000` | 900,010 | 18,969 | 150 ms |
| **Keyset** at position 900,000 | **10** | **4** | **0.06 ms** |

Keyset is **~2,500× faster** at depth 900k, and its cost does not depend on position. The OFFSET column grows exactly linearly with `k`. That is the giveaway: the "rows read" number is `offset + limit`.

This is a *best case* for OFFSET. Three things make it worse in production.

**1. The heap is not in index order.** The table above was bulk-loaded in `created_at` order, so `pg_stats.correlation = -1`: consecutive index entries point into the same heap page, and 900k rows cost only ~19k page visits. The same rows in shuffled physical order (`correlation ≈ 0`, which is typical after months of updates, deletes and HOT churn) cost:

| Query (shuffled heap) | Rows read | Buffers touched | Time |
|---|---:|---:|---:|
| `OFFSET 100000` | 100,010 | 100,389 | 98 ms |
| `OFFSET 900000` | 900,010 | **903,406** | **739 ms** |
| Keyset at 900,000 | 10 | 13 | 0.16 ms |

That is one buffer access per discarded row. On a cold cache each of those is a random read. At 100 µs per NVMe read, `OFFSET 900000` becomes a ~90 s query that also evicts your hot working set from `shared_buffers`.

**2. No index matches the `ORDER BY`.** Without an index on the sort key, every page is a full scan plus a sort. Page 1 uses a cheap *top-N heapsort* (it only keeps `n` rows). A deep page has to keep `offset + n` rows. Once that exceeds `work_mem` (4 MB default), the whole input is **sorted on disk**:

| `ORDER BY amount_cents DESC, id DESC` (no index) | Sort method | Time |
|---|---|---:|
| `LIMIT 10` | top-N heapsort, 27 kB | 61 ms |
| `LIMIT 10 OFFSET 100000` | **external merge, ~110 MB on disk** | 182 ms |
| `LIMIT 10 OFFSET 900000` | **external merge, ~110 MB on disk** | 474 ms |

**3. The "total" in the envelope.** `{"total": 1543}` looks free, but it is a `COUNT(*)` on every request. On this table it costs 55 ms (a parallel index-only scan over 1M rows), which is **more than 1,000× the cost of the page itself**. On a filtered, bloated or larger table it is worse, and PostgreSQL has no cached row count (MVCC means each transaction may see a different count).

**Deferred join ("late row lookup")** is the standard mitigation when you can't remove OFFSET. Skip through a covering index with an index-only scan, then fetch the full rows for just the `n` surviving IDs:

```sql
SELECT o.*
FROM orders o
JOIN (SELECT id FROM orders
      ORDER BY created_at DESC, id DESC
      LIMIT 10 OFFSET 900000) page USING (id)
ORDER BY o.created_at DESC, o.id DESC;
```

| `OFFSET 900000` | Plain | Deferred join |
|---|---:|---:|
| Correlated heap | 150 ms / 18,969 buffers | 120 ms / 3,498 buffers |
| Shuffled heap | 739 ms / 903,406 buffers | **128 ms / 3,498 buffers** |

The deferred join removes the heap I/O (5.8× faster on a realistic heap and 258× fewer buffers), but it is still `O(offset)` because the index-only scan walks 900k entries. It also depends on the visibility map being current (`VACUUM`). Otherwise "index-only" scans fall back to heap fetches. Treat it as a mitigation for legacy offset APIs, not a fix.

> Full methodology and the storage-engine view: [SQL Performance Deep Dive, Anti-pattern 5](../databases/15-sql-performance-deep-dive.md#4-performance-anti-patterns).

#### 3.4.3 Correctness: OFFSET drifts while data changes

Performance is the argument everyone knows. The correctness bug is the one that ships. An offset is a position, so any insert or delete before that position shifts every later page.

```text
feed sorted newest-first, page size 3, ids 1..10

GET ?offset=0  → 10, 9, 8
    (two new rows, 11 and 12, are inserted)
GET ?offset=3  → 9, 8, 7     ← 9 and 8 shown twice
    keyset: WHERE (created_at, id) < (ts_of_8, 8)
           → 7, 6, 5         ← correct
```

A delete before the offset shifts rows the other way, and the client **silently misses** a row. For a feed this is cosmetic. For a sync or export job ("copy all invoices to the ERP") it is data loss. Keyset is anchored to a *value*, not a position, so concurrent writes before the cursor cannot shift it.

#### 3.4.4 Keyset pagination done right (the SQL rules)

Each rule below comes from a bug seen in production.

**Rule 1: The sort key must be unique. Always add a tie-breaker.** `ORDER BY created_at` alone is non-deterministic when timestamps collide (bulk imports, coarse clock resolution, the same millisecond). The `WHERE created_at < $last` predicate then **drops** every row that shares the boundary timestamp. Append the primary key: `ORDER BY created_at DESC, id DESC`.

**Rule 2: Use row-value comparison, not the expanded `OR` (PostgreSQL).** These two predicates are logically equivalent, but they run very differently:

```sql
-- ✅ 0.06 ms: single Index Cond, B-tree seeks straight to the cursor
WHERE (created_at, id) < ($1, $2)

-- ❌ 124 ms: planner treats it as a Filter; index walked from the start
WHERE created_at < $1 OR (created_at = $1 AND id < $2)

-- ✅ 0.06 ms: if you must use OR (e.g. ORMs), add the redundant bound
WHERE created_at <= $1 AND (created_at < $1 OR id < $2)
```

Measured at position 900k on the table above. The naive `OR` form reads 10 rows but touches **18,969 buffers**, the same as `OFFSET 900000`. Your keyset pagination is then just OFFSET under another name. Always check that the cursor predicate appears as `Index Cond`, not `Filter`, in `EXPLAIN`.

**MySQL/InnoDB is the opposite.** The optimizer has known gaps with row-constructor *inequalities* ([manual](https://dev.mysql.com/doc/refman/8.4/en/row-constructor-optimization.html), [bug #111952](https://bugs.mysql.com/bug.php?id=111952)). Use the expanded form there, with the redundant leading bound, and check `key_len` in `EXPLAIN`.

**Rule 3: The index must match filter + sort exactly.** Equality filters come first, then the sort columns in the same direction:

```sql
-- endpoint: GET /customers/{id}/orders  (newest first)
CREATE INDEX orders_customer_created_id ON orders (customer_id, created_at DESC, id DESC);
```

| `WHERE customer_id = 42 ORDER BY created_at DESC, id DESC LIMIT 10` | Plan | Time |
|---|---|---:|
| Only `(created_at, id)` index exists | Parallel Seq Scan + Sort, 1M rows filtered | 25 ms |
| `(customer_id, created_at, id)` index | Index Scan, 10 rows | **0.03 ms** |

Keyset without the matching index is still a full scan, even on page 1. Every filter you expose (status, tenant, owner) needs its index or must be a cheap post-filter on a selective one. That is a strong argument for **a small, fixed set of sortable/filterable fields** rather than arbitrary `?sort=any_column`.

**Rule 4: Mixed sort directions break row comparison.** `ORDER BY priority DESC, id ASC` can't be written as one `(a, b) < (x, y)`. Either make the directions match (`id DESC` is usually fine as a tie-breaker), or expand with the redundant bound: `priority <= $p AND (priority < $p OR id > $id)`, with the index declared as `(priority DESC, id ASC)`.

**Rule 5: Nullable sort columns need a total order.** `NULL` is neither `<` nor `>` anything, so rows with `NULL` vanish from every page after the first. Sort on `COALESCE(col, sentinel)` with a matching expression index, declare the column `NOT NULL`, or handle the null partition explicitly (`NULLS LAST` and a second phase).

**Rule 6: Sort on immutable columns for sync/export.** If the client paginates by `updated_at` and a row is updated mid-walk, it moves *behind* the cursor and is skipped in this pass. For "give me everything that changed" use a monotonic, never-updated key (`id`, a sequence-backed `change_seq`, or a CDC/outbox log), and treat `updated_at` only as a hint.

**Rule 7: Previous page = flip and reverse.** Seek in the opposite direction, then reverse in the application:

```sql
-- prev page: rows just *newer* than the first row on the current page
SELECT * FROM orders
WHERE (created_at, id) > ($first_created_at, $first_id)
ORDER BY created_at ASC, id ASC            -- same index, scanned backwards
LIMIT 20;                                  -- then reverse() before returning
```

#### 3.4.5 Opaque cursors: the API contract

Never put raw column names and values in the public API (`?after_created_at=…&after_id=…`). It couples clients to your schema and blocks you from changing the sort key. Clients will also construct their own values. A JS client that round-trips a microsecond timestamp through `Date` truncates it to milliseconds, and the page boundary then silently drops rows.

Encode the position into an **opaque token** that only the server reads:

| Put in the token | Why |
|---|---|
| Sort-key values of the last row + the tie-breaker ID | The seek position |
| Format version (`v`) | Lets you change the encoding or sort key without breaking in-flight tokens |
| Hash of filters + sort order | Reject a token reused with different filters ([Google AIP-158](https://google.aip.dev/158)), so pages from two different result sets never mix |
| Direction (next/prev), optional expiry | Bidirectional paging; bound how long a snapshot is honored |
| **HMAC signature** | Clients can't forge positions or probe other tenants' key ranges. Encrypt instead (AES-GCM) if the values themselves are sensitive |

Reference implementation (stdlib + psycopg 3), verified against PostgreSQL 16 with the tests listed under *Notes*:

```python
import base64, hashlib, hmac, json
from datetime import datetime

PAGE_SIZE_DEFAULT, PAGE_SIZE_MAX = 20, 100
_SIG_LEN = 16  # truncated HMAC-SHA256


class InvalidPageToken(ValueError):
    """Map to HTTP 400 INVALID_ARGUMENT."""


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _filters_hash(filters: dict) -> str:
    canonical = json.dumps(filters, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def encode_token(key: bytes, created_at: datetime, row_id: int, filters: dict) -> str:
    payload = json.dumps(
        {"v": 1, "t": created_at.isoformat(), "id": row_id, "f": _filters_hash(filters)},
        separators=(",", ":"),
    ).encode()
    return _b64e(payload + hmac.new(key, payload, hashlib.sha256).digest()[:_SIG_LEN])


def decode_token(key: bytes, token: str, filters: dict) -> tuple[datetime, int]:
    try:
        raw = _b64d(token)
    except ValueError as e:
        raise InvalidPageToken("malformed page_token") from e
    payload, sig = raw[:-_SIG_LEN], raw[-_SIG_LEN:]
    expected = hmac.new(key, payload, hashlib.sha256).digest()[:_SIG_LEN]
    if len(raw) <= _SIG_LEN or not hmac.compare_digest(sig, expected):
        raise InvalidPageToken("page_token signature mismatch")
    data = json.loads(payload)
    if data.get("v") != 1:
        raise InvalidPageToken("unsupported page_token version")
    if data["f"] != _filters_hash(filters):
        raise InvalidPageToken("page_token does not match the request filters")
    return datetime.fromisoformat(data["t"]), int(data["id"])


def list_orders(conn, key: bytes, *, status: str | None = None,
                page_size: int = PAGE_SIZE_DEFAULT, page_token: str | None = None) -> dict:
    page_size = max(1, min(page_size, PAGE_SIZE_MAX))       # clamp, never trust the client
    filters = {"status": status}
    where, params = [], {"limit": page_size + 1}            # n+1 → has_more without COUNT(*)
    if status is not None:
        where.append("status = %(status)s")
        params["status"] = status
    if page_token:
        params["c_t"], params["c_id"] = decode_token(key, page_token, filters)
        where.append("(created_at, id) < (%(c_t)s, %(c_id)s)")  # Index Cond, not Filter
    sql = ("SELECT id, status, amount_cents, created_at FROM orders"
           + (" WHERE " + " AND ".join(where) if where else "")
           + " ORDER BY created_at DESC, id DESC LIMIT %(limit)s")
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    has_more = len(rows) > page_size
    rows = rows[:page_size]
    next_token = encode_token(key, rows[-1][3], rows[-1][0], filters) if has_more else None
    return {"data": rows, "next_page_token": next_token}
```

Notes:
- The signing key comes from the secret store and is rotated like any other key. During rotation, accept tokens signed with both the old and new key (`kid` in the payload).
- SQL is built only from fixed fragments; every value is a bind parameter. Never interpolate a client-supplied sort column. Map `?order_by=` through an allow-list to a pre-written `ORDER BY` + index pair.
- Tests worth having: a full walk visits every row exactly once (with many duplicate timestamps), an insert between pages causes no duplicates, a tampered token returns 400, a token reused with different filters returns 400, an oversized `page_size` is clamped.

#### 3.4.6 Response envelope

Pick one shape and use it on every list endpoint:

```json
{
  "data": [ ... ],
  "next_page_token": "eyJ2IjoxLCJ0Ijoi...",
  "has_more": true
}
```

How the major APIs do it:

| API | Request | Response | Notes |
|---|---|---|---|
| **Google (AIP-158)** | `page_size`, `page_token` | `next_page_token` (empty = done) | Token is opaque; changing other params invalidates it; server may return fewer than `page_size` |
| **Stripe** | `limit` (1–100), `starting_after` / `ending_before` = object ID | `has_more`, `data` | The cursor is the last object's ID; the server looks up its sort position |
| **Slack** | `limit`, `cursor` | `response_metadata.next_cursor` | Migrated from offset to cursor ([Slack Engineering](https://slack.engineering/evolving-api-pagination-at-slack/)); empty cursor = done |
| **GitHub REST** | `per_page` (≤100), `page` or `before`/`after` | `Link: <…>; rel="next"` header | Clients must follow `rel="next"` rather than build URLs |
| **GraphQL / Relay** | `first`, `after` | `edges { cursor }`, `pageInfo { hasNextPage, endCursor }` | The de-facto GraphQL standard |

The `Link` header ([RFC 8288](https://www.rfc-editor.org/rfc/rfc8288)) is a good addition to the body field. Generic HTTP clients and CLIs can follow it without knowing your schema.

**Totals, without paying `COUNT(*)` on every page:**

| Option | Cost | Use when |
|---|---|---|
| Omit the total; return `has_more` (from `LIMIT n+1`) | Free | Default: feeds, infinite scroll, sync |
| Capped count: `SELECT count(*) FROM (SELECT 1 … LIMIT 10001) s` → "10,000+" | ≤ 10k rows | UI wants a number but not an exact one |
| Estimate: `pg_class.reltuples` or the planner's `EXPLAIN` row estimate | ~0 | "About 1.2M results" |
| Exact count as a separate, cached endpoint (`GET /orders:count`) | One scan, cached | Reports and billing; the client asks for it explicitly |

#### 3.4.7 "But users need page 47"

Product often asks for numbered pages. The options, in order of preference:

1. **Challenge the requirement.** Very few people click page 47. They search, filter or sort instead. Search engines cap deep paging for this reason: Elasticsearch rejects `from + size > 10,000` by default (`index.max_result_window`). Google Search stops after a few hundred results.
2. **Offset with a hard depth cap.** Allow `page` only while `offset ≤ 10,000` (or whatever your p99 budget allows from the table in 3.4.2). Beyond the cap, return `400` with a hint to narrow the filter or switch to the cursor.
3. **Offset + deferred join + covering index** for the legacy endpoint you can't change (see 3.4.2).
4. **Coarse jump anchors.** Precompute a value boundary every N rows (for example, the `created_at` of every 1,000th row, refreshed periodically), seek to the anchor, then page with keyset. This gives "approximately page 47" at keyset cost.
5. **Search index for deep, filtered browsing.** Use `search_after` plus a point-in-time (PIT) snapshot in Elasticsearch/OpenSearch, which is keyset over a consistent view. Don't use `from/size`, and don't use `scroll` for user-facing paging (it is no longer recommended for deep pagination).

#### 3.4.8 Bulk export and sync are not pagination

When the goal is "all of it" (exports, backfills, mirrors), page-by-page HTTP adds latency on every round trip and holds DB snapshots for a long time.

- **Small (< ~100k rows):** keyset by primary key (`WHERE id > $last ORDER BY id LIMIT 1000`). This is cheap, restartable and order-stable.
- **Large:** an async export job (`POST /exports` → `202` + job URL → signed object-storage URL). The job streams with a server-side cursor or `COPY`, outside the request path.
- **Incremental sync:** a change feed (`GET /events?after=<seq>`) backed by a monotonic sequence or CDC/outbox, not `updated_at` (see Rule 6).

#### 3.4.9 Security and abuse

- **Clamp `page_size`** (default 20, max 100, as Stripe, GitHub and Google do). A missing max turns one request into a full-table dump.
- **Validate and sign cursors.** Return `400 INVALID_ARGUMENT` (or `invalid_cursor`) on a bad token. Never `500`, and never "start over from page 1" silently.
- **Tenant scoping lives in the `WHERE`, not in the token.** The token holds a position, never an authorization decision. Always re-apply `tenant_id = $caller_tenant`.
- **Deep offsets are a DoS vector.** `?offset=50000000` is a cheap request that triggers an expensive query. Cap the depth, set `statement_timeout` on the API's DB role, and rate-limit by cost, not just by request count.
- **Don't leak counts or IDs.** Sequential IDs in cursors let clients enumerate or estimate volume. Use signed or encrypted tokens and non-sequential public IDs (see [7.11](#711-security-anti-patterns)).

#### 3.4.10 Decision checklist

- [ ] Collection can grow unbounded → cursor (keyset inside), not offset
- [ ] Sort key is unique (tie-breaker = PK) and immutable for sync use cases
- [ ] Composite index = equality filters + sort columns in the same direction; `EXPLAIN` shows the cursor as `Index Cond`
- [ ] PostgreSQL: row-value `(a, b) < (x, y)`; MySQL: expanded form with a redundant leading bound
- [ ] Token is opaque, versioned, signed, and bound to filters/sort; bad token → 400
- [ ] `has_more` via `LIMIT n+1`; no `COUNT(*)` on the hot path
- [ ] `page_size` clamped; offset (if offered) depth-capped; `statement_timeout` set
- [ ] Exports and syncs use async jobs or change feeds, not page walking

---

### 3.5 Embedded Entity vs Linked Entity

**When to Embed:**
- Data is always needed together
- Reduces round trips
- Small, bounded data

**When to Link:**
- Large related resources
- Data that changes independently
- Circular references

```json
{
  "order": {
    "id": "ord_123",

    "customer": {
      "id": "cust_456",
      "name": "John Doe"
    },

    "customer_url": "/api/v1/customers/cust_456",

    "line_items": [
      {
        "product_id": "prod_789",
        "product_url": "/api/v1/products/prod_789",
        "quantity": 2
      }
    ]
  }
}
```

---

## 4. Quality Patterns

### 4.1 Conditional Request

**Intent:** Optimize bandwidth and prevent conflicts using HTTP conditional headers.

**For Caching (GET requests):**
```http
# First request
GET /api/v1/products/123

Response:
HTTP/1.1 200 OK
ETag: "abc123"
Last-Modified: Wed, 21 Jun 2023 07:28:00 GMT
Cache-Control: max-age=3600

# Subsequent request
GET /api/v1/products/123
If-None-Match: "abc123"

Response:
HTTP/1.1 304 Not Modified
```

**For Optimistic Locking (PUT/PATCH requests):**
```http
PUT /api/v1/products/123
If-Match: "abc123"
Content-Type: application/json

{
  "name": "Updated Product",
  "price": 29.99
}

# If resource changed:
HTTP/1.1 412 Precondition Failed
{
  "error": "conflict",
  "message": "Resource was modified. Fetch latest version and retry.",
  "current_etag": "def456"
}
```

---

### 4.2 Request Bundle (Batch Operations)

**Intent:** Reduce network overhead by combining multiple operations.

**Google Batch API:**
```http
POST /batch
Content-Type: multipart/mixed; boundary=batch_abc123

--batch_abc123
Content-Type: application/http
Content-ID: <item1>

GET /api/v1/users/1

--batch_abc123
Content-Type: application/http
Content-ID: <item2>

GET /api/v1/users/2

--batch_abc123--
```

**JSON Batch Pattern:**
```http
POST /api/v1/batch
Content-Type: application/json

{
  "requests": [
    {
      "id": "req1",
      "method": "GET",
      "path": "/users/1"
    },
    {
      "id": "req2",
      "method": "POST",
      "path": "/orders",
      "body": {"customer_id": "1", "items": [...]}
    }
  ]
}

Response:
{
  "responses": [
    {"id": "req1", "status": 200, "body": {...}},
    {"id": "req2", "status": 201, "body": {...}}
  ]
}
```

---

### 4.3 Wish List / Sparse Fieldset

**Intent:** Allow clients to request only the fields they need.

**Google Standard (Field Masks):**
```http
GET /api/v1/users/123?fields=id,name,email

# For updates - only update specified fields
PATCH /api/v1/users/123?updateMask=name,email
{
  "name": "New Name",
  "email": "new@example.com"
}
```

**GraphQL Approach:**
```graphql
query {
  user(id: "123") {
    id
    name
    email
    orders(first: 5) {
      id
      total
    }
  }
}
```

**JSON:API Sparse Fieldsets:**
```http
GET /api/v1/articles?include=author&fields[articles]=title,body&fields[people]=name
```

---

### 4.4 Wish Template / Expansion

**Intent:** Allow clients to request embedded related resources.

```http
GET /api/v1/orders/123?expand=customer,line_items.product

Response:
{
  "id": "ord_123",
  "customer": {
    "id": "cust_456",
    "name": "John Doe",
    "email": "john@example.com"
  },
  "line_items": [
    {
      "id": "li_1",
      "product": {
        "id": "prod_789",
        "name": "Widget",
        "price": 29.99
      },
      "quantity": 2
    }
  ]
}
```

**Stripe Expansion:**
```bash
curl https://api.stripe.com/v1/charges/ch_123?expand[]=customer&expand[]=invoice.subscription
```

---

### 4.5 Rate Limiting

**Intent:** Protect APIs from abuse and ensure fair usage.

**Headers Standard:**
```http
HTTP/1.1 200 OK
X-RateLimit-Limit: 1000
X-RateLimit-Remaining: 998
X-RateLimit-Reset: 1623456789
X-RateLimit-Reset-After: 3600

# When rate limited:
HTTP/1.1 429 Too Many Requests
Retry-After: 60
{
  "error": {
    "code": "rate_limit_exceeded",
    "message": "Rate limit exceeded. Retry after 60 seconds.",
    "retry_after": 60
  }
}
```

**GitHub Rate Limiting:**
```http
X-RateLimit-Limit: 5000
X-RateLimit-Remaining: 4999
X-RateLimit-Used: 1
X-RateLimit-Reset: 1623456789
X-RateLimit-Resource: core
```

---

### 4.6 Error Reporting

**Intent:** Provide actionable error information.

**RFC 7807 Problem Details:**
```json
{
  "type": "https://api.example.com/errors/insufficient-funds",
  "title": "Insufficient Funds",
  "status": 422,
  "detail": "Account balance of $10.00 is less than required $25.00",
  "instance": "/api/v1/transfers/txn_abc123",
  "balance": {
    "current": 1000,
    "required": 2500,
    "currency": "USD"
  },
  "trace_id": "abc123xyz"
}
```

**Google Error Standard:**
```json
{
  "error": {
    "code": 400,
    "message": "Invalid argument",
    "status": "INVALID_ARGUMENT",
    "details": [
      {
        "@type": "type.googleapis.com/google.rpc.BadRequest",
        "fieldViolations": [
          {
            "field": "email",
            "description": "Invalid email format"
          }
        ]
      }
    ]
  }
}
```

**Stripe Error Standard:**
```json
{
  "error": {
    "type": "card_error",
    "code": "card_declined",
    "message": "Your card was declined.",
    "param": "card_number",
    "decline_code": "insufficient_funds",
    "doc_url": "https://stripe.com/docs/error-codes/card-declined"
  }
}
```

---

### 4.7 Context Representation (Idempotency)

**Intent:** Enable safe retries for non-idempotent operations.

```http
POST /api/v1/payments
Idempotency-Key: unique-request-id-abc123
Content-Type: application/json

{
  "amount": 1000,
  "currency": "USD",
  "customer_id": "cust_456"
}

# Retry with same key returns cached response
POST /api/v1/payments
Idempotency-Key: unique-request-id-abc123

Response:
HTTP/1.1 200 OK
Idempotency-Replayed: true
{
  "id": "pay_789",
  "amount": 1000,
  "status": "succeeded"
}
```

**Stripe Idempotency:**
```bash
curl https://api.stripe.com/v1/charges \
  -H "Idempotency-Key: $(uuidgen)" \
  -d amount=2000 \
  -d currency=usd
```

---

## 5. Evolution Patterns

### 5.1 Two in Production

**Intent:** Run multiple API versions simultaneously during migration.

```
┌─────────────────────────────────────────────────────┐
│                   API Gateway                        │
├─────────────────────────────────────────────────────┤
│  /v1/*  →  Legacy Service (maintenance mode)        │
│  /v2/*  →  New Service (active development)         │
└─────────────────────────────────────────────────────┘
```

**Deprecation Timeline:**
```http
# Response headers for deprecated endpoints
HTTP/1.1 200 OK
Deprecation: Sun, 01 Jan 2024 00:00:00 GMT
Sunset: Sun, 01 Jul 2024 00:00:00 GMT
Link: </api/v2/users>; rel="successor-version"
```

---

### 5.2 Experimental Preview

**Intent:** Release new features for early feedback before commitment.

```http
# Opt-in to experimental features
GET /api/v1/users
X-API-Preview: new-pagination

# Or via query parameter
GET /api/v1/users?preview=new-pagination
```

**GitHub Previews:**
```bash
curl -H "Accept: application/vnd.github.scarlet-witch-preview+json" \
  https://api.github.com/repos/owner/repo
```

---

### 5.3 Aggressive Obsolescence

**Intent:** Clearly communicate and enforce deprecation timelines.

```json
{
  "data": {...},
  "_warnings": [
    {
      "code": "deprecated_field",
      "message": "Field 'legacy_id' is deprecated. Use 'id' instead.",
      "deprecated_at": "2023-01-01",
      "sunset_at": "2024-01-01",
      "migration": "https://docs.example.com/migrate-legacy-id"
    }
  ]
}
```

---

## 6. Big Tech API Standards

### 6.1 Google API Design Guide

**Core Principles:**
1. **Resource-Oriented Design:** APIs are modeled as resource hierarchies
2. **Standard Methods:** Use standard HTTP methods (GET, POST, PUT, DELETE)
3. **Custom Methods:** Use POST with `:action` suffix for non-CRUD operations

**Resource Naming:**
```
// Collection: publishers
// Resource: publishers/{publisher}
// Sub-collection: publishers/{publisher}/books
// Sub-resource: publishers/{publisher}/books/{book}

GET /v1/publishers/123/books/456
```

**Standard Methods Mapping:**

| Method | HTTP | URI | Description |
|--------|------|-----|-------------|
| List | GET | /resources | List all resources |
| Get | GET | /resources/{id} | Get a resource |
| Create | POST | /resources | Create a resource |
| Update | PUT/PATCH | /resources/{id} | Update a resource |
| Delete | DELETE | /resources/{id} | Delete a resource |

**Field Naming:**
```json
{
  "display_name": "My Book",
  "create_time": "2023-06-15T10:30:00Z",
  "update_time": "2023-06-20T14:22:00Z",
  "delete_time": null,
  "expire_time": "2024-06-15T10:30:00Z",
  "page_size": 20,
  "page_token": "abc123"
}
```

---

### 6.2 Microsoft REST API Guidelines

**Core Principles:**
1. **Consistency:** APIs should be consistent and predictable
2. **Simplicity:** Simple things should be simple, complex things possible
3. **Evolvability:** APIs should evolve without breaking clients

**URL Structure:**
```
https://{service}.microsoft.com/api/{version}/{collection}/{id}
```

**Response Format:**
```json
{
  "@odata.context": "https://graph.microsoft.com/v1.0/$metadata#users",
  "@odata.count": 50,
  "@odata.nextLink": "https://graph.microsoft.com/v1.0/users?$skiptoken=abc",
  "value": [
    {
      "id": "user-id",
      "displayName": "John Doe",
      "mail": "john@example.com"
    }
  ]
}
```

**Error Format:**
```json
{
  "error": {
    "code": "BadRequest",
    "message": "Invalid filter clause",
    "target": "$filter",
    "details": [],
    "innererror": {
      "code": "InvalidFilterClause",
      "message": "Filter expression is invalid"
    }
  }
}
```

---

### 6.3 AWS API Standards

**Naming Conventions:**
```
# Service endpoint pattern
https://{service}.{region}.amazonaws.com

# Action-based (older style)
POST / HTTP/1.1
Content-Type: application/x-www-form-urlencoded
Action=DescribeInstances&Version=2016-11-15

# REST-based (newer services)
GET /2015-03-31/functions/myFunction HTTP/1.1
Host: lambda.us-east-1.amazonaws.com
```

**Request Signing (Signature V4):**
```
Authorization: AWS4-HMAC-SHA256
  Credential=AKIAIOSFODNN7EXAMPLE/20230615/us-east-1/s3/aws4_request,
  SignedHeaders=host;x-amz-content-sha256;x-amz-date,
  Signature=abc123...
```

**Pagination:**
```json
{
  "Items": [...],
  "Count": 25,
  "LastEvaluatedKey": {
    "pk": "USER#123",
    "sk": "ORDER#456"
  }
}
```

---

### 6.4 Stripe API Standards

**Design Principles:**
1. **Predictability:** Consistent patterns across all endpoints
2. **Explicit over implicit:** Clear, verbose field names
3. **Idempotency:** All write operations support idempotency keys

**Object Structure:**
```json
{
  "id": "cus_abc123",
  "object": "customer",
  "created": 1623456789,
  "livemode": true,
  "metadata": {
    "order_id": "12345"
  },
  "name": "John Doe",
  "email": "john@example.com",
  "balance": 0,
  "default_source": "card_xyz789"
}
```

**Expandable Fields:**
```bash
# Request
curl https://api.stripe.com/v1/charges/ch_123?expand[]=customer

# Response - customer is expanded inline
{
  "id": "ch_123",
  "customer": {
    "id": "cus_456",
    "name": "John Doe"
  }
}
```

**Event-Driven (Webhooks):**
```json
{
  "id": "evt_abc123",
  "object": "event",
  "api_version": "2023-10-16",
  "created": 1623456789,
  "type": "customer.created",
  "data": {
    "object": {...}
  },
  "livemode": true,
  "pending_webhooks": 1
}
```

---

### 6.5 Twitter/X API Standards

**Response Envelope:**
```json
{
  "data": [{
    "id": "1234567890",
    "text": "Hello world!",
    "created_at": "2023-06-15T10:30:00.000Z"
  }],
  "includes": {
    "users": [{
      "id": "user_123",
      "name": "John Doe",
      "username": "johndoe"
    }]
  },
  "meta": {
    "result_count": 10,
    "next_token": "abc123"
  }
}
```

**Field Selection:**
```http
GET /2/tweets?ids=123,456&tweet.fields=created_at,author_id&expansions=author_id&user.fields=username
```

---

## 7. Anti-Patterns

### 7.1 Chatty API

**Problem:** Requiring many round trips to accomplish a single task.

```http
# Anti-pattern: Multiple calls needed
GET /api/users/123
GET /api/users/123/address
GET /api/users/123/preferences
GET /api/users/123/orders
GET /api/users/123/orders/latest

# Pattern: Composite endpoint or expansion
GET /api/users/123?expand=address,preferences,orders.latest

# Or: GraphQL
query {
  user(id: "123") {
    name
    address { ... }
    preferences { ... }
    orders(first: 1) { ... }
  }
}
```

**Impact:**
- High latency (network round trips)
- Increased load on servers
- Poor mobile experience

---

### 7.2 Anemic API

**Problem:** API that exposes data but not behaviors.

```http
# Anti-pattern: Client must know business logic
GET /api/orders/123
# Client calculates: total = sum(items.price * items.quantity) + tax + shipping

PATCH /api/orders/123
{
  "status": "shipped"
}
# Client must know all valid status transitions

# Pattern: Expose behaviors as resources/actions
POST /api/orders/123:calculate-total
POST /api/orders/123:ship
POST /api/orders/123:refund
```

---

### 7.3 God Object API

**Problem:** Single endpoint that does too much.

```http
# Anti-pattern
POST /api/process
{
  "action": "create_user_and_send_email_and_subscribe_to_newsletter",
  "user": {...},
  "email_template": "welcome",
  "newsletter_id": "weekly"
}

# Pattern: Separate concerns
POST /api/users
POST /api/users/123/welcome-email
POST /api/newsletters/weekly/subscribers
```

---

### 7.4 Leaky Abstraction

**Problem:** Exposing internal implementation details.

```http
# Anti-pattern: Database schema leaked
{
  "user_id": 123,
  "user_tbl_created_dt": "2023-06-15",
  "fk_address_id": 456,
  "internal_status_code": 1
}

# Pattern: Clean domain model
{
  "id": "usr_abc123",
  "created_at": "2023-06-15T10:30:00Z",
  "address": {
    "id": "addr_def456"
  },
  "status": "active"
}
```

---

### 7.5 Breaking Changes Without Versioning

**Problem:** Making incompatible changes without warning.

**Breaking Changes Include:**
- Removing fields
- Renaming fields
- Changing field types
- Changing URL paths
- Modifying required parameters
- Changing error codes
- Altering authentication

```http
# Anti-pattern
# v1: GET /api/users/123 returns { "name": "John" }
# Later: GET /api/users/123 returns { "full_name": "John" }

# Pattern: Additive changes + deprecation period
{
  "name": "John",
  "full_name": "John Doe",
  "_deprecations": [{
    "field": "name",
    "replacement": "full_name",
    "sunset": "2024-01-01"
  }]
}
```

---

### 7.6 Inconsistent Naming

**Problem:** Mixed naming conventions across the API.

```json
// Anti-pattern: Mixed conventions
{
  "user_id": "123",
  "userName": "johndoe",
  "EmailAddress": "john@example.com",
  "created-at": "2023-06-15"
}

// Pattern: Consistent snake_case (or camelCase - pick one)
{
  "user_id": "123",
  "user_name": "johndoe",
  "email_address": "john@example.com",
  "created_at": "2023-06-15T10:30:00Z"
}
```

---

### 7.7 Missing Pagination

**Problem:** Returning unbounded result sets.

```http
# Anti-pattern
GET /api/users
# Returns all 1 million users

# Pattern: Always paginate collections (opaque token, clamped size, no total)
GET /api/users?page_size=20&page_token=abc

{
  "data": [...],
  "next_page_token": "def456",
  "has_more": true
}
```

Don't fix the unbounded response by adding `"total_count": 1000000` to every page. That is a `COUNT(*)` per request, often more expensive than the page itself. See [3.4 Pagination Patterns](#34-pagination-patterns) for the cost model, keyset SQL rules and alternatives to exact totals.

---

### 7.8 Ignoring HTTP Semantics

**Problem:** Using wrong HTTP methods and status codes.

```http
# Anti-pattern
POST /api/getUser?id=123
POST /api/deleteUser?id=123
GET /api/users (returns 200 with error in body)

# Pattern: Proper HTTP usage
GET /api/users/123
DELETE /api/users/123
GET /api/users
# Returns 404 for not found, 500 for errors
```

**HTTP Method Semantics:**

| Method | Idempotent | Safe | Cacheable | Body |
|--------|------------|------|-----------|------|
| GET | Yes | Yes | Yes | No |
| HEAD | Yes | Yes | Yes | No |
| POST | No | No | No* | Yes |
| PUT | Yes | No | No | Yes |
| PATCH | No | No | No | Yes |
| DELETE | Yes | No | No | Optional |

---

### 7.9 Over-Engineering

**Problem:** Adding unnecessary complexity.

```http
# Anti-pattern: Over-engineered for simple CRUD
POST /api/v2/enterprises/ent_123/contexts/prod/resources/users/actions/create
{
  "meta": {
    "request_id": "...",
    "correlation_id": "...",
    "trace_parent": "..."
  },
  "data": {
    "type": "user",
    "attributes": {
      "name": "John"
    }
  }
}

# Pattern: Simple and direct
POST /api/users
{
  "name": "John"
}
```

---

### 7.10 N+1 Query Problem in API Design

**Problem:** API design that encourages N+1 data fetching.

```http
# Anti-pattern: Forces N+1 queries
GET /api/orders
[
  {"id": 1, "customer_id": 100},
  {"id": 2, "customer_id": 101},
  {"id": 3, "customer_id": 102}
]
# Client must make N additional calls for customer details

# Pattern 1: Support expansion
GET /api/orders?expand=customer

# Pattern 2: Support batch endpoints
POST /api/customers/batch
{
  "ids": [100, 101, 102]
}

# Pattern 3: Use GraphQL data loader pattern
query {
  orders {
    id
    customer {  # Batched automatically
      name
    }
  }
}
```

---

### 7.11 Security Anti-Patterns

**Exposing Sensitive Data:**
```json
// Anti-pattern
{
  "id": "usr_123",
  "password_hash": "$2b$12$abc...",
  "ssn": "123-45-6789",
  "internal_flags": ["admin", "test_user"]
}

// Pattern: Filter sensitive fields
{
  "id": "usr_123",
  "email": "j***@example.com",
  "ssn_last_four": "6789"
}
```

**Sequential IDs:**
```http
# Anti-pattern: Predictable IDs enable enumeration
GET /api/users/1
GET /api/users/2
GET /api/users/3

# Pattern: Use UUIDs or prefixed random IDs
GET /api/users/usr_7Ks9xN2mP
```

---

### 7.12 Poor Error Handling

```json
// Anti-pattern: Unhelpful errors
{
  "error": true,
  "message": "Error"
}

// Anti-pattern: Exposing stack traces
{
  "error": "NullPointerException at com.example.UserService.getUser(UserService.java:123)"
}

// Pattern: Actionable, safe errors
{
  "error": {
    "code": "user_not_found",
    "message": "User with ID 'usr_123' was not found",
    "documentation_url": "https://docs.example.com/errors/user_not_found",
    "request_id": "req_abc123"
  }
}
```

---

## 8. Best Practices Checklist

### Design Phase
- [ ] Use resource-oriented design (nouns, not verbs)
- [ ] Apply consistent naming conventions (snake_case or camelCase)
- [ ] Design for evolvability (versioning strategy)
- [ ] Document API with OpenAPI/AsyncAPI
- [ ] Define clear error codes and messages

### Implementation Phase
- [ ] Use appropriate HTTP methods and status codes
- [ ] Implement pagination for all collections
- [ ] Support filtering, sorting, and field selection
- [ ] Add ETags and conditional request handling
- [ ] Implement idempotency for write operations
- [ ] Add rate limiting with proper headers

### Security Phase
- [ ] Use HTTPS everywhere
- [ ] Implement proper authentication (OAuth 2.0, API keys)
- [ ] Validate all inputs
- [ ] Use non-sequential, random IDs
- [ ] Never expose sensitive data or internal errors
- [ ] Implement request signing for sensitive operations

### Operations Phase
- [ ] Include request IDs in all responses
- [ ] Log requests with correlation IDs
- [ ] Monitor latency, error rates, throughput
- [ ] Set up alerts for anomalies
- [ ] Document deprecation timelines
- [ ] Provide migration guides for breaking changes

---

## References

1. Zimmermann, O., Stocker, M., Lübke, D., Zdun, U., & Pautasso, C. (2022). *Patterns for API Design: Simplifying Integration with Loosely Coupled Message Exchanges*. Addison-Wesley.

2. Google Cloud API Design Guide: https://cloud.google.com/apis/design

3. Microsoft REST API Guidelines: https://github.com/microsoft/api-guidelines

4. Stripe API Reference: https://stripe.com/docs/api

5. AWS API Design: https://docs.aws.amazon.com/

6. JSON:API Specification: https://jsonapi.org/

7. RFC 7807 - Problem Details for HTTP APIs: https://tools.ietf.org/html/rfc7807

8. OpenAPI Specification: https://spec.openapis.org/oas/latest.html

9. Google AIP-158, Pagination: https://google.aip.dev/158

10. Markus Winand, *No Offset* (Use The Index, Luke): https://use-the-index-luke.com/no-offset

11. PostgreSQL docs, Row Constructor Comparison: https://www.postgresql.org/docs/current/functions-comparisons.html#ROW-WISE-COMPARISON

12. MySQL docs, Range Optimization of Row Constructor Expressions: https://dev.mysql.com/doc/refman/8.4/en/row-constructor-optimization.html

13. Slack Engineering, Evolving API Pagination at Slack: https://slack.engineering/evolving-api-pagination-at-slack/

14. Elasticsearch, Paginate search results (`search_after`, PIT, `max_result_window`): https://www.elastic.co/docs/reference/elasticsearch/rest-apis/paginate-search-results

15. RFC 8288, Web Linking (`Link` header): https://www.rfc-editor.org/rfc/rfc8288
