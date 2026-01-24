# Database Design Best Practices & Anti-Patterns

A comprehensive guide for senior and staff-level engineers covering schema design trade-offs, database selection (SQL vs NoSQL vs NewSQL), modern databases (2025), vector databases, big tech architectures, scaling strategies, and common pitfalls.

---

## Table of Contents

### Part 1: Schema Design & Best Practices
1. [Database Schema Design Trade-offs](#1-database-schema-design-trade-offs)
2. [Normalization vs Denormalization](#2-normalization-vs-denormalization)
3. [Boolean Flags vs Nullable Timestamps](#3-boolean-flags-vs-nullable-timestamps)
4. [Many-to-Many Relationships with Attributes](#4-many-to-many-relationships-with-attributes)
5. [Audit Fields & State Fields](#5-audit-fields--state-fields)
6. [Operational vs Analytical Schema Design](#6-operational-vs-analytical-schema-design-oltp-vs-olap)
7. [String Codes vs Foreign Keys](#7-string-codes-vs-foreign-keys)
8. [Rate Limiting Schema Design](#8-rate-limiting-schema-design)
9. [Primary Key Strategies: UUID vs Auto-Increment](#9-primary-key-strategies-uuid-vs-auto-increment)
10. [Soft Delete vs Hard Delete](#10-soft-delete-vs-hard-delete)
11. [Polymorphic Associations](#11-polymorphic-associations-anti-pattern)
12. [Temporal Data & History Tables](#12-temporal-data--history-tables)
13. [Concurrency Control: Optimistic vs Pessimistic Locking](#13-concurrency-control-optimistic-vs-pessimistic-locking)
14. [Database Indexing Strategies](#14-database-indexing-strategies)
15. [NULL Handling & Three-Valued Logic](#15-null-handling--three-valued-logic)
16. [Sharding & Partitioning](#16-sharding--partitioning)
17. [Common Anti-Patterns to Avoid](#17-common-anti-patterns-to-avoid)
17B. [SQL Antipatterns: Bill Karwin's Complete Reference](#17b-sql-antipatterns-bill-karwins-complete-reference)

### Part 2: Database Selection & Scaling (2025)
18. [Database Selection Guide: When to Use What](#18-database-selection-guide-when-to-use-what)
19. [Modern Databases (2025): Specialized Solutions](#19-modern-databases-2025-specialized-solutions)
20. [Big Tech Database Architectures](#20-big-tech-database-architectures)
21. [Vector Databases for AI/ML Applications](#21-vector-databases-for-aiml-applications)
22. [Database Scaling Strategies: Startup to Enterprise](#22-database-scaling-strategies-startup-to-enterprise)
23. [Anti-Patterns in Database Selection](#23-anti-patterns-in-database-selection)

### Part 3: Modern PostgreSQL Ecosystem (2025)
24. [Serverless PostgreSQL Platforms](#24-serverless-postgresql-platforms-neon-supabase-planetscale)
25. [Modern PostgreSQL Extensions](#25-modern-postgresql-extensions-2025)
26. [Connection Pooling: PgBouncer vs PgCat vs Supavisor](#26-connection-pooling-pgbouncer-vs-pgcat-vs-supavisor)
27. [Local-First & Edge Databases](#27-local-first--edge-databases)
28. [PostgreSQL Storage Engines & Future](#28-postgresql-storage-engines--future)

---

## 1. Database Schema Design Trade-offs

### Core Principles

Database schema design involves balancing competing concerns. Every decision has trade-offs:

| Concern | Trade-off |
|---------|-----------|
| **Read Performance** vs **Write Performance** | Denormalization speeds reads but slows writes |
| **Data Integrity** vs **Performance** | Constraints ensure integrity but add overhead |
| **Flexibility** vs **Structure** | Loose schemas adapt easily but lose validation |
| **Storage** vs **Speed** | Redundancy uses space but eliminates joins |

### Indexing Trade-offs

Indexes improve read performance at the cost of write performance and storage:

```sql
-- Index speeds up this query
CREATE INDEX idx_users_email ON users(email);

-- But every INSERT/UPDATE now has additional overhead
INSERT INTO users (email, name) VALUES ('user@example.com', 'John');
```

**Guidelines:**
- Profile before adding indexes - don't add preemptively
- Remove unused indexes regularly
- Consider partial indexes for specific query patterns
- Use covering indexes to avoid table lookups

### Foreign Key Trade-offs

```sql
-- Foreign keys ensure referential integrity
ALTER TABLE orders ADD CONSTRAINT fk_customer
    FOREIGN KEY (customer_id) REFERENCES customers(id);
```

**Pros:**
- Database-enforced data integrity
- Prevents orphaned records
- Self-documenting relationships

**Cons:**
- Validation overhead on every insert/update
- At very large scale, some companies drop them for write performance
- Must be enforced at application level if dropped

### Primary Key Selection

| Type | Storage | Performance | Scalability | Security |
|------|---------|-------------|-------------|----------|
| Auto-increment INT | 4 bytes | Best for single-node | Poor for distributed | Exposes record count |
| Auto-increment BIGINT | 8 bytes | Good for single-node | Poor for distributed | Exposes record count |
| UUID v4 | 16 bytes | Poor (random I/O) | Excellent | Excellent |
| UUID v7 | 16 bytes | Good (time-ordered) | Excellent | Excellent |
| Snowflake ID | 8 bytes | Good | Excellent | Good |

---

## 2. Normalization vs Denormalization

### When to Normalize (OLTP / Transactional Systems)

Normalization reduces redundancy by organizing data into smaller, well-structured tables following normal forms (1NF through BCNF).

```sql
-- Normalized design: separate tables for entities
CREATE TABLE customers (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    email VARCHAR(255) UNIQUE NOT NULL
);

CREATE TABLE orders (
    id SERIAL PRIMARY KEY,
    customer_id INTEGER REFERENCES customers(id),
    order_date TIMESTAMP NOT NULL,
    total_amount DECIMAL(10,2) NOT NULL
);

CREATE TABLE order_items (
    id SERIAL PRIMARY KEY,
    order_id INTEGER REFERENCES orders(id),
    product_id INTEGER REFERENCES products(id),
    quantity INTEGER NOT NULL,
    unit_price DECIMAL(10,2) NOT NULL
);
```

**Use normalization when:**
- Write performance and data accuracy are critical
- Data changes frequently (social media profiles, inventory)
- ACID compliance is required
- Storage optimization matters

### When to Denormalize (OLAP / Analytical Systems)

Denormalization reintroduces controlled redundancy to improve read performance.

```sql
-- Denormalized design: pre-joined data
CREATE TABLE order_summary (
    order_id INTEGER PRIMARY KEY,
    customer_name VARCHAR(100),
    customer_email VARCHAR(255),
    order_date TIMESTAMP,
    total_amount DECIMAL(10,2),
    item_count INTEGER,
    product_names TEXT[]  -- Array of product names
);
```

**Use denormalization when:**
- Read performance is critical (dashboards, reports)
- Data is queried frequently but rarely updated
- Complex joins are causing performance bottlenecks
- Building OLAP or data warehouse systems

### Performance Benchmarks

- Complex analytical queries: **10-15x improvement** on denormalized structures
- Column-oriented storage: additional **3-5x gains** for aggregations

### Hybrid Approach (Recommended)

```sql
-- Start normalized, denormalize hotspots
-- Option 1: Materialized Views
CREATE MATERIALIZED VIEW order_analytics AS
SELECT
    o.id,
    c.name as customer_name,
    o.order_date,
    COUNT(oi.id) as item_count,
    SUM(oi.quantity * oi.unit_price) as total
FROM orders o
JOIN customers c ON o.customer_id = c.id
JOIN order_items oi ON oi.order_id = o.id
GROUP BY o.id, c.name, o.order_date;

-- Refresh periodically
REFRESH MATERIALIZED VIEW order_analytics;
```

**Best Practice:** Start with normalized schema, identify bottlenecks through profiling, then selectively denormalize those parts.

---

## 3. Boolean Flags vs Nullable Timestamps

### The Anti-Pattern: Boolean Flags

```sql
-- Anti-pattern: boolean flags
CREATE TABLE articles (
    id SERIAL PRIMARY KEY,
    title VARCHAR(255),
    content TEXT,
    is_published BOOLEAN DEFAULT FALSE,
    is_deleted BOOLEAN DEFAULT FALSE,
    is_featured BOOLEAN DEFAULT FALSE
);
```

**Problems with boolean flags:**
- No temporal information (when did this happen?)
- Cannot distinguish between "never set" and "set to false"
- Leads to accumulation of flags over time

### The Pattern: Nullable Timestamps

```sql
-- Better: nullable timestamps
CREATE TABLE articles (
    id SERIAL PRIMARY KEY,
    title VARCHAR(255),
    content TEXT,
    published_at TIMESTAMP NULL,
    deleted_at TIMESTAMP NULL,
    featured_at TIMESTAMP NULL
);

-- Querying is just as simple
SELECT * FROM articles WHERE published_at IS NOT NULL;
SELECT * FROM articles WHERE deleted_at IS NULL;  -- Not deleted
```

**Advantages:**
- Get the "when" for free (audit trail)
- Same boolean functionality via `IS NULL` / `IS NOT NULL`
- Frameworks already support this (e.g., `deleted_at` for soft deletes)
- Storage cost is negligible (4 bytes boolean vs 8 bytes timestamp)

### When Boolean Flags Are Still Appropriate

```sql
-- Two-way toggles that can flip back and forth
-- Need both "when activated" AND "when deactivated"
CREATE TABLE memberships (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    is_active BOOLEAN DEFAULT TRUE,
    activated_at TIMESTAMP,
    deactivated_at TIMESTAMP
);

-- Distinction between NULL and FALSE matters
CREATE TABLE preferences (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    email_notifications BOOLEAN NULL  -- NULL = use default, FALSE = explicitly disabled
);
```

### Migration Example

```sql
-- Migrating from boolean to timestamp
ALTER TABLE articles ADD COLUMN published_at TIMESTAMP;

UPDATE articles
SET published_at = updated_at
WHERE is_published = TRUE;

ALTER TABLE articles DROP COLUMN is_published;
```

---

## 4. Many-to-Many Relationships with Attributes

### Basic Junction Table

```sql
-- Basic many-to-many: students <-> courses
CREATE TABLE enrollments (
    student_id INTEGER NOT NULL REFERENCES students(id),
    course_id INTEGER NOT NULL REFERENCES courses(id),
    PRIMARY KEY (student_id, course_id)
);
```

### Junction Table with Attributes (Associative Entity)

When the relationship itself has properties, the junction table becomes a first-class entity:

```sql
CREATE TABLE enrollments (
    id SERIAL PRIMARY KEY,  -- Optional: add surrogate key
    student_id INTEGER NOT NULL REFERENCES students(id),
    course_id INTEGER NOT NULL REFERENCES courses(id),

    -- Relationship attributes
    enrolled_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    grade VARCHAR(5),
    status VARCHAR(20) NOT NULL DEFAULT 'active',
    dropped_at TIMESTAMP,
    drop_reason TEXT,

    -- Audit fields
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    -- Constraints
    UNIQUE (student_id, course_id),  -- Alternative to composite PK
    CHECK (status IN ('active', 'completed', 'dropped', 'failed'))
);

-- Index foreign keys for join performance
CREATE INDEX idx_enrollments_student ON enrollments(student_id);
CREATE INDEX idx_enrollments_course ON enrollments(course_id);
CREATE INDEX idx_enrollments_status ON enrollments(status);
```

### Best Practices

1. **Use Composite Primary Key OR Surrogate Key + Unique Constraint**
   ```sql
   -- Option A: Composite PK (simpler, smaller indexes)
   PRIMARY KEY (student_id, course_id)

   -- Option B: Surrogate PK (easier ORM mapping, enables updates)
   id SERIAL PRIMARY KEY,
   UNIQUE (student_id, course_id)
   ```

2. **Always Index Foreign Keys**
   ```sql
   CREATE INDEX idx_enrollments_student ON enrollments(student_id);
   CREATE INDEX idx_enrollments_course ON enrollments(course_id);
   ```

3. **Add Timestamps for Auditing**
   ```sql
   enrolled_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
   ```

4. **Use Descriptive Table Names**
   - `enrollments` not `students_courses`
   - `order_items` not `orders_products`

5. **Plan for Future Attributes**
   - Junction tables often grow to include more attributes
   - Design with extensibility in mind

### Real-World Example: E-commerce Order Items

```sql
CREATE TABLE order_items (
    id SERIAL PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id),

    -- Relationship attributes
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_price DECIMAL(10,2) NOT NULL,  -- Snapshot at time of order
    discount_percent DECIMAL(5,2) DEFAULT 0,
    notes TEXT,

    -- Fulfillment tracking
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    shipped_at TIMESTAMP,
    tracking_number VARCHAR(100),

    UNIQUE (order_id, product_id)
);
```

---

## 5. Audit Fields & State Fields

### Standard Audit Fields

Every table should include basic audit metadata:

```sql
CREATE TABLE orders (
    id SERIAL PRIMARY KEY,
    -- Business fields
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    total_amount DECIMAL(10,2) NOT NULL,

    -- Standard audit fields
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_by INTEGER REFERENCES users(id),
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_by INTEGER REFERENCES users(id),

    -- Version for optimistic locking
    version INTEGER NOT NULL DEFAULT 1
);

-- Auto-update updated_at with trigger
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER orders_updated_at
    BEFORE UPDATE ON orders
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at();
```

### Extended Audit Fields for Compliance

```sql
CREATE TABLE financial_transactions (
    id SERIAL PRIMARY KEY,
    -- Business fields...

    -- Basic audit
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_by INTEGER NOT NULL REFERENCES users(id),
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_by INTEGER REFERENCES users(id),

    -- Extended audit for compliance
    ip_address INET,
    user_agent TEXT,
    change_reason TEXT,  -- Required for some regulatory contexts
    approved_at TIMESTAMP,
    approved_by INTEGER REFERENCES users(id)
);
```

### State Fields and State Machines

```sql
-- Option 1: Enum type (PostgreSQL)
CREATE TYPE order_status AS ENUM (
    'pending', 'confirmed', 'processing',
    'shipped', 'delivered', 'cancelled', 'refunded'
);

CREATE TABLE orders (
    id SERIAL PRIMARY KEY,
    status order_status NOT NULL DEFAULT 'pending',
    -- ...
);

-- Option 2: Lookup table (more flexible)
CREATE TABLE order_statuses (
    code VARCHAR(20) PRIMARY KEY,
    name VARCHAR(50) NOT NULL,
    description TEXT,
    is_terminal BOOLEAN DEFAULT FALSE,
    display_order INTEGER
);

INSERT INTO order_statuses (code, name, is_terminal, display_order) VALUES
('pending', 'Pending', FALSE, 1),
('confirmed', 'Confirmed', FALSE, 2),
('processing', 'Processing', FALSE, 3),
('shipped', 'Shipped', FALSE, 4),
('delivered', 'Delivered', TRUE, 5),
('cancelled', 'Cancelled', TRUE, 6),
('refunded', 'Refunded', TRUE, 7);

CREATE TABLE orders (
    id SERIAL PRIMARY KEY,
    status_code VARCHAR(20) NOT NULL DEFAULT 'pending'
        REFERENCES order_statuses(code),
    -- ...
);
```

### State Transition History

```sql
-- Track all state changes
CREATE TABLE order_status_history (
    id SERIAL PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    from_status VARCHAR(20),
    to_status VARCHAR(20) NOT NULL,
    changed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    changed_by INTEGER REFERENCES users(id),
    reason TEXT,
    metadata JSONB
);

CREATE INDEX idx_order_status_history_order ON order_status_history(order_id);
CREATE INDEX idx_order_status_history_changed_at ON order_status_history(changed_at);
```

### Choosing Status Field Storage

| Approach | Pros | Cons |
|----------|------|------|
| **ENUM** | Type-safe, efficient storage | Hard to add values (requires ALTER) |
| **VARCHAR with CHECK** | Flexible, easy to modify | No compile-time safety |
| **Foreign Key to lookup** | Self-documenting, extensible | Extra join for labels |
| **TINYINT** | Most efficient storage | Requires app-level mapping |

**Recommendation:** Use lookup tables for statuses that change independently of deployments. Use ENUMs or CHECK constraints for truly fixed values.

---

## 6. Operational vs Analytical Schema Design (OLTP vs OLAP)

### OLTP: Operational Schema Design

Optimized for transactional workloads: frequent reads and writes of small amounts of data.

```sql
-- Highly normalized schema for OLTP
CREATE TABLE customers (
    id SERIAL PRIMARY KEY,
    email VARCHAR(255) UNIQUE NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE customer_profiles (
    customer_id INTEGER PRIMARY KEY REFERENCES customers(id),
    first_name VARCHAR(50),
    last_name VARCHAR(50),
    phone VARCHAR(20),
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE addresses (
    id SERIAL PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    type VARCHAR(20) NOT NULL,  -- 'billing', 'shipping'
    street VARCHAR(200),
    city VARCHAR(100),
    state VARCHAR(50),
    postal_code VARCHAR(20),
    country CHAR(2)
);

CREATE TABLE orders (
    id SERIAL PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    shipping_address_id INTEGER REFERENCES addresses(id),
    billing_address_id INTEGER REFERENCES addresses(id),
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

**OLTP Characteristics:**
- Normalized to 3NF or higher
- Row-oriented storage
- ACID transactions
- Optimized for point queries and small range scans
- Indexes on frequently queried columns

### OLAP: Analytical Schema Design

Optimized for complex analytical queries over large datasets.

#### Star Schema

```sql
-- Dimension tables (denormalized)
CREATE TABLE dim_customer (
    customer_key SERIAL PRIMARY KEY,
    customer_id INTEGER NOT NULL,  -- Natural key
    email VARCHAR(255),
    full_name VARCHAR(100),
    city VARCHAR(100),
    state VARCHAR(50),
    country VARCHAR(50),
    customer_segment VARCHAR(50),
    -- Slowly Changing Dimension (SCD Type 2)
    effective_date DATE NOT NULL,
    expiration_date DATE,
    is_current BOOLEAN DEFAULT TRUE
);

CREATE TABLE dim_product (
    product_key SERIAL PRIMARY KEY,
    product_id INTEGER NOT NULL,
    product_name VARCHAR(200),
    category VARCHAR(100),
    subcategory VARCHAR(100),
    brand VARCHAR(100),
    unit_price DECIMAL(10,2)
);

CREATE TABLE dim_date (
    date_key INTEGER PRIMARY KEY,  -- YYYYMMDD
    full_date DATE NOT NULL,
    year INTEGER,
    quarter INTEGER,
    month INTEGER,
    month_name VARCHAR(20),
    week INTEGER,
    day_of_week INTEGER,
    day_name VARCHAR(20),
    is_weekend BOOLEAN,
    is_holiday BOOLEAN
);

-- Fact table (aggregated measures)
CREATE TABLE fact_sales (
    sale_key SERIAL PRIMARY KEY,
    date_key INTEGER NOT NULL REFERENCES dim_date(date_key),
    customer_key INTEGER NOT NULL REFERENCES dim_customer(customer_key),
    product_key INTEGER NOT NULL REFERENCES dim_product(product_key),

    -- Measures
    quantity INTEGER NOT NULL,
    unit_price DECIMAL(10,2) NOT NULL,
    discount_amount DECIMAL(10,2) DEFAULT 0,
    total_amount DECIMAL(10,2) NOT NULL,

    -- Degenerate dimension
    order_number VARCHAR(50)
);

-- Indexes optimized for analytical queries
CREATE INDEX idx_fact_sales_date ON fact_sales(date_key);
CREATE INDEX idx_fact_sales_customer ON fact_sales(customer_key);
CREATE INDEX idx_fact_sales_product ON fact_sales(product_key);
```

**OLAP Characteristics:**
- Denormalized (star or snowflake schema)
- Column-oriented storage often used
- Optimized for aggregations and full table scans
- Pre-aggregated summary tables
- Minimal joins needed for common queries

### ETL Pipeline: OLTP to OLAP

```sql
-- Example ETL to populate fact table
INSERT INTO fact_sales (date_key, customer_key, product_key,
                        quantity, unit_price, discount_amount, total_amount, order_number)
SELECT
    TO_CHAR(o.created_at, 'YYYYMMDD')::INTEGER as date_key,
    dc.customer_key,
    dp.product_key,
    oi.quantity,
    oi.unit_price,
    oi.discount_amount,
    oi.quantity * oi.unit_price - oi.discount_amount as total_amount,
    o.order_number
FROM orders o
JOIN order_items oi ON oi.order_id = o.id
JOIN dim_customer dc ON dc.customer_id = o.customer_id AND dc.is_current = TRUE
JOIN dim_product dp ON dp.product_id = oi.product_id
WHERE o.created_at >= :last_etl_run;
```

### Comparison Summary

| Aspect | OLTP | OLAP |
|--------|------|------|
| **Purpose** | Day-to-day transactions | Business intelligence |
| **Schema** | Normalized (3NF+) | Denormalized (Star/Snowflake) |
| **Queries** | Simple, point lookups | Complex aggregations |
| **Data** | Current state | Historical |
| **Updates** | Frequent | Batch loads |
| **Users** | Many concurrent | Few analysts |

---

## 7. String Codes vs Foreign Keys

### When to Use Foreign Keys with Lookup Tables

```sql
-- Lookup table approach
CREATE TABLE countries (
    id SERIAL PRIMARY KEY,
    code CHAR(2) UNIQUE NOT NULL,  -- ISO 3166-1 alpha-2
    name VARCHAR(100) NOT NULL
);

CREATE TABLE addresses (
    id SERIAL PRIMARY KEY,
    country_id INTEGER REFERENCES countries(id),
    -- ...
);

-- Query requires join
SELECT a.*, c.name as country_name
FROM addresses a
JOIN countries c ON c.id = a.country_id;
```

**Use lookup tables when:**
- Values have additional attributes (name, description, display order)
- List has more than 10-15 values
- Values change independently of deployments
- Referential integrity is critical
- You need to query/filter by the lookup attributes

### When to Store String Codes Directly

```sql
-- Direct string storage
CREATE TABLE addresses (
    id SERIAL PRIMARY KEY,
    country_code CHAR(2) NOT NULL CHECK (country_code ~ '^[A-Z]{2}$'),
    -- ...
);

-- Query is simpler - no join needed
SELECT * FROM addresses WHERE country_code = 'US';
```

**Use string codes when:**
- Values are well-known standards (ISO codes, HTTP status codes)
- List is small and stable (< 10-15 values)
- Additional metadata isn't needed
- Performance is critical (eliminates join)
- Values are meaningful without lookup (e.g., 'USD', 'US', 'M'/'F')

### Hybrid Approach

```sql
-- Store the code, but also maintain a reference table for validation
CREATE TABLE currency_codes (
    code CHAR(3) PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    symbol VARCHAR(5)
);

CREATE TABLE transactions (
    id SERIAL PRIMARY KEY,
    amount DECIMAL(15,2) NOT NULL,
    currency_code CHAR(3) NOT NULL REFERENCES currency_codes(code),
    -- ...
);

-- No join needed for most queries
SELECT amount, currency_code FROM transactions;

-- Join only when you need the currency name
SELECT t.amount, t.currency_code, c.name, c.symbol
FROM transactions t
JOIN currency_codes c ON c.code = t.currency_code;
```

### Anti-Pattern: One True Lookup Table (OTLT)

```sql
-- ANTI-PATTERN: Generic lookup table
CREATE TABLE lookups (
    id SERIAL PRIMARY KEY,
    type VARCHAR(50) NOT NULL,
    code VARCHAR(50) NOT NULL,
    value VARCHAR(200),
    UNIQUE (type, code)
);

INSERT INTO lookups (type, code, value) VALUES
('country', 'US', 'United States'),
('country', 'CA', 'Canada'),
('status', 'active', 'Active'),
('status', 'inactive', 'Inactive');
```

**Problems with OTLT:**
- Cannot enforce referential integrity properly
- Different "types" may need different attributes
- Indexes are less efficient
- Query optimizer can't optimize as well
- Invites dirty data over time

---

## 8. Rate Limiting Schema Design

### Fixed Window Counter Schema

```sql
CREATE TABLE rate_limits (
    id SERIAL PRIMARY KEY,
    -- Identifier (user, API key, IP)
    identifier VARCHAR(255) NOT NULL,
    identifier_type VARCHAR(20) NOT NULL,  -- 'user_id', 'api_key', 'ip'

    -- Window definition
    window_start TIMESTAMP NOT NULL,
    window_duration_seconds INTEGER NOT NULL,

    -- Counter
    request_count INTEGER NOT NULL DEFAULT 0,

    -- Configuration
    max_requests INTEGER NOT NULL,

    UNIQUE (identifier, identifier_type, window_start)
);

CREATE INDEX idx_rate_limits_lookup
    ON rate_limits(identifier, identifier_type, window_start);
```

### Sliding Window Log Schema

```sql
-- For precise rate limiting (more storage, better accuracy)
CREATE TABLE request_log (
    id SERIAL PRIMARY KEY,
    identifier VARCHAR(255) NOT NULL,
    identifier_type VARCHAR(20) NOT NULL,
    requested_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    endpoint VARCHAR(200)
);

CREATE INDEX idx_request_log_lookup
    ON request_log(identifier, identifier_type, requested_at);

-- Query to check rate limit
SELECT COUNT(*)
FROM request_log
WHERE identifier = :id
  AND identifier_type = :type
  AND requested_at > NOW() - INTERVAL '1 minute';

-- Cleanup old records periodically
DELETE FROM request_log
WHERE requested_at < NOW() - INTERVAL '1 hour';
```

### Rate Limit Configuration Table

```sql
CREATE TABLE rate_limit_rules (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,

    -- What this rule applies to
    identifier_type VARCHAR(20) NOT NULL,  -- 'user_id', 'api_key', 'ip', 'global'
    endpoint_pattern VARCHAR(200),  -- NULL = all endpoints, or '/api/v1/*'

    -- Limits
    max_requests INTEGER NOT NULL,
    window_seconds INTEGER NOT NULL,

    -- Behavior
    burst_allowance INTEGER DEFAULT 0,  -- Extra requests allowed in burst

    -- Priority (lower = higher priority)
    priority INTEGER NOT NULL DEFAULT 100,

    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Example rules
INSERT INTO rate_limit_rules (name, identifier_type, endpoint_pattern, max_requests, window_seconds, priority)
VALUES
    ('Global API Limit', 'api_key', NULL, 1000, 3600, 100),
    ('Auth Endpoint Limit', 'ip', '/api/auth/*', 10, 60, 50),
    ('Premium User Limit', 'user_id', NULL, 10000, 3600, 100);
```

### Production Recommendation: Use Redis

For high-traffic systems, relational databases are not ideal for rate limiting:

```python
# Redis-based rate limiting (sliding window)
import redis
import time

def is_rate_limited(redis_client, identifier: str, max_requests: int, window_seconds: int) -> bool:
    key = f"rate_limit:{identifier}"
    now = time.time()
    window_start = now - window_seconds

    pipe = redis_client.pipeline()
    # Remove old entries
    pipe.zremrangebyscore(key, 0, window_start)
    # Count current window
    pipe.zcard(key)
    # Add current request
    pipe.zadd(key, {str(now): now})
    # Set expiry
    pipe.expire(key, window_seconds)

    results = pipe.execute()
    current_count = results[1]

    return current_count >= max_requests
```

### Relational DB for Configuration, Redis for Counters

```sql
-- Store configuration in PostgreSQL
CREATE TABLE rate_limit_config (
    id SERIAL PRIMARY KEY,
    tier VARCHAR(50) NOT NULL,  -- 'free', 'pro', 'enterprise'
    endpoint_category VARCHAR(50),
    requests_per_minute INTEGER NOT NULL,
    requests_per_hour INTEGER NOT NULL,
    requests_per_day INTEGER NOT NULL
);

-- Application reads config from DB, enforces limits via Redis
```

---

## 9. Primary Key Strategies: UUID vs Auto-Increment

### Auto-Increment (Sequential IDs)

```sql
CREATE TABLE users (
    id SERIAL PRIMARY KEY,  -- PostgreSQL
    -- id INT AUTO_INCREMENT PRIMARY KEY,  -- MySQL
    email VARCHAR(255) UNIQUE NOT NULL
);
```

**Pros:**
- Smallest storage (4 or 8 bytes)
- Best index performance (sequential inserts)
- Human-readable
- Natural ordering by creation time

**Cons:**
- Exposes business data (total record count)
- Cannot generate IDs client-side
- Doesn't work in distributed systems (collision risk)
- Predictable (security concern for public APIs)

### UUID v4 (Random)

```sql
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email VARCHAR(255) UNIQUE NOT NULL
);
```

**Pros:**
- Globally unique
- Can be generated anywhere (client, server, database)
- Safe for public APIs (unpredictable)
- Works in distributed systems

**Cons:**
- 16 bytes storage (4x larger)
- Random I/O causes index fragmentation (especially MySQL)
- Not human-readable
- No natural ordering

### UUID v7 (Time-Ordered) - Recommended for New Projects

```sql
-- PostgreSQL with pg_uuidv7 extension
CREATE EXTENSION IF NOT EXISTS pg_uuidv7;

CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
    email VARCHAR(255) UNIQUE NOT NULL
);
```

**Pros:**
- Globally unique like UUID v4
- Time-ordered (reduces index fragmentation)
- Sortable by creation time
- Best of both worlds

**Cons:**
- Still 16 bytes
- Newer standard (less library support)
- Coarse time precision visible in ID

### Comparison Table

| Criterion | Auto-Inc | UUID v4 | UUID v7 | Snowflake |
|-----------|----------|---------|---------|-----------|
| **Size** | 4-8 bytes | 16 bytes | 16 bytes | 8 bytes |
| **Index Performance** | Excellent | Poor | Good | Good |
| **Distributed** | No | Yes | Yes | Yes |
| **Client Generation** | No | Yes | Yes | Requires service |
| **Ordering** | Yes | No | Yes | Yes |
| **Security** | Poor | Excellent | Good | Good |

### Recommendation

- **Single database, internal IDs:** Auto-increment
- **Public APIs, distributed systems:** UUID v7 or Snowflake
- **Legacy systems, MySQL with clustered PK:** Consider UUID v7 or keep auto-increment

---

## 10. Soft Delete vs Hard Delete

### Soft Delete Implementation

```sql
CREATE TABLE users (
    id SERIAL PRIMARY KEY,
    email VARCHAR(255) NOT NULL,
    name VARCHAR(100),

    -- Soft delete field
    deleted_at TIMESTAMP NULL,

    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Partial unique index (only non-deleted records must be unique)
CREATE UNIQUE INDEX idx_users_email_active
    ON users(email) WHERE deleted_at IS NULL;

-- Query active users (default view)
CREATE VIEW active_users AS
SELECT * FROM users WHERE deleted_at IS NULL;

-- Soft delete
UPDATE users SET deleted_at = CURRENT_TIMESTAMP WHERE id = :id;

-- Restore
UPDATE users SET deleted_at = NULL WHERE id = :id;
```

### When to Use Soft Delete

**Pros:**
- Data recovery is trivial
- Maintains foreign key integrity
- Audit trail preserved
- Can amortize expensive deletes to background jobs

**Cons:**
- Database bloat over time
- Every query must filter `deleted_at IS NULL`
- Unique constraints become complex
- Recovery often doesn't work in practice (dangling references)

### When to Use Hard Delete

```sql
-- Simple hard delete
DELETE FROM users WHERE id = :id;
```

**Pros:**
- Clean database, no bloat
- Simple queries
- GDPR "right to erasure" compliance
- Normal unique constraints work

**Cons:**
- No recovery without backups
- Can break foreign key relationships
- No audit trail (unless logged separately)

### Archive Table Pattern (Best of Both Worlds)

```sql
-- Main table (active records only)
CREATE TABLE users (
    id SERIAL PRIMARY KEY,
    email VARCHAR(255) UNIQUE NOT NULL,
    name VARCHAR(100),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Archive table (deleted records)
CREATE TABLE users_archive (
    id INTEGER NOT NULL,  -- Not SERIAL, preserves original ID
    email VARCHAR(255) NOT NULL,
    name VARCHAR(100),
    created_at TIMESTAMP NOT NULL,

    -- Archive metadata
    archived_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    archived_by INTEGER,
    archive_reason TEXT
);

-- Delete procedure
CREATE OR REPLACE FUNCTION archive_user(user_id INTEGER, reason TEXT DEFAULT NULL)
RETURNS VOID AS $$
BEGIN
    INSERT INTO users_archive (id, email, name, created_at, archive_reason)
    SELECT id, email, name, created_at, reason
    FROM users WHERE id = user_id;

    DELETE FROM users WHERE id = user_id;
END;
$$ LANGUAGE plpgsql;
```

### Decision Matrix

| Requirement | Soft Delete | Hard Delete | Archive |
|-------------|-------------|-------------|---------|
| Easy recovery | Yes | No | Yes |
| Database size | Grows | Stable | Separate |
| Query simplicity | Complex | Simple | Simple |
| Unique constraints | Complex | Simple | Simple |
| GDPR compliance | Difficult | Easy | Easy |
| Audit trail | Built-in | Requires logging | Yes |

---

## 11. Polymorphic Associations (Anti-Pattern)

### The Anti-Pattern

```sql
-- ANTI-PATTERN: Polymorphic association
CREATE TABLE comments (
    id SERIAL PRIMARY KEY,
    body TEXT NOT NULL,

    -- Polymorphic reference - AVOID THIS
    commentable_id INTEGER NOT NULL,
    commentable_type VARCHAR(50) NOT NULL,  -- 'Post', 'Photo', 'Video'

    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Cannot create proper foreign key!
-- This query is inefficient:
SELECT * FROM comments
WHERE commentable_type = 'Post' AND commentable_id = 123;
```

**Problems:**
1. **No referential integrity** - Database cannot enforce foreign keys
2. **No join optimization** - Optimizer can't use FK relationships
3. **Wasted storage** - Type column stores repeated strings
4. **Error-prone** - Typos in type strings go undetected
5. **Complex queries** - Always need to filter by both columns

### Solution 1: Separate Join Tables

```sql
-- Better: Separate tables for each relationship
CREATE TABLE posts (
    id SERIAL PRIMARY KEY,
    title VARCHAR(200) NOT NULL,
    body TEXT
);

CREATE TABLE photos (
    id SERIAL PRIMARY KEY,
    url VARCHAR(500) NOT NULL,
    caption TEXT
);

CREATE TABLE post_comments (
    id SERIAL PRIMARY KEY,
    post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    body TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE photo_comments (
    id SERIAL PRIMARY KEY,
    photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    body TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

### Solution 2: Shared Base Table

```sql
-- Shared base entity
CREATE TABLE commentables (
    id SERIAL PRIMARY KEY,
    type VARCHAR(20) NOT NULL CHECK (type IN ('post', 'photo', 'video'))
);

CREATE TABLE posts (
    id INTEGER PRIMARY KEY REFERENCES commentables(id),
    title VARCHAR(200) NOT NULL,
    body TEXT
);

CREATE TABLE photos (
    id INTEGER PRIMARY KEY REFERENCES commentables(id),
    url VARCHAR(500) NOT NULL,
    caption TEXT
);

CREATE TABLE comments (
    id SERIAL PRIMARY KEY,
    commentable_id INTEGER NOT NULL REFERENCES commentables(id) ON DELETE CASCADE,
    body TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Proper foreign key enforced!
```

### Solution 3: Exclusive Belongs-To (Multiple Nullable FKs)

```sql
CREATE TABLE comments (
    id SERIAL PRIMARY KEY,

    -- Only one should be non-null
    post_id INTEGER REFERENCES posts(id) ON DELETE CASCADE,
    photo_id INTEGER REFERENCES photos(id) ON DELETE CASCADE,
    video_id INTEGER REFERENCES videos(id) ON DELETE CASCADE,

    body TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    -- Constraint: exactly one must be set
    CONSTRAINT exactly_one_parent CHECK (
        (post_id IS NOT NULL)::int +
        (photo_id IS NOT NULL)::int +
        (video_id IS NOT NULL)::int = 1
    )
);

-- Create partial indexes for each type
CREATE INDEX idx_comments_post ON comments(post_id) WHERE post_id IS NOT NULL;
CREATE INDEX idx_comments_photo ON comments(photo_id) WHERE photo_id IS NOT NULL;
CREATE INDEX idx_comments_video ON comments(video_id) WHERE video_id IS NOT NULL;
```

---

## 12. Temporal Data & History Tables

### System-Versioned Temporal Tables (SQL:2011)

```sql
-- SQL Server / MariaDB syntax
CREATE TABLE products (
    id INT PRIMARY KEY,
    name VARCHAR(200) NOT NULL,
    price DECIMAL(10,2) NOT NULL,

    -- Temporal columns
    valid_from DATETIME2 GENERATED ALWAYS AS ROW START,
    valid_to DATETIME2 GENERATED ALWAYS AS ROW END,

    PERIOD FOR SYSTEM_TIME (valid_from, valid_to)
) WITH (SYSTEM_VERSIONING = ON (HISTORY_TABLE = dbo.products_history));

-- Query current data
SELECT * FROM products;

-- Query historical data
SELECT * FROM products FOR SYSTEM_TIME AS OF '2024-01-01';

-- Query all versions
SELECT * FROM products FOR SYSTEM_TIME ALL;
```

### Manual History Table Implementation (PostgreSQL)

```sql
-- Main table
CREATE TABLE products (
    id SERIAL PRIMARY KEY,
    name VARCHAR(200) NOT NULL,
    price DECIMAL(10,2) NOT NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- History table
CREATE TABLE products_history (
    history_id SERIAL PRIMARY KEY,
    id INTEGER NOT NULL,  -- Original product ID
    name VARCHAR(200) NOT NULL,
    price DECIMAL(10,2) NOT NULL,

    -- Temporal validity
    valid_from TIMESTAMP NOT NULL,
    valid_to TIMESTAMP NOT NULL,

    -- Audit info
    operation CHAR(1) NOT NULL,  -- 'I', 'U', 'D'
    changed_by INTEGER,
    changed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_products_history_id ON products_history(id);
CREATE INDEX idx_products_history_valid ON products_history(id, valid_from, valid_to);

-- Trigger to maintain history
CREATE OR REPLACE FUNCTION products_audit_trigger()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        INSERT INTO products_history (id, name, price, valid_from, valid_to, operation)
        VALUES (OLD.id, OLD.name, OLD.price, OLD.updated_at, NEW.updated_at, 'U');
        RETURN NEW;
    ELSIF TG_OP = 'DELETE' THEN
        INSERT INTO products_history (id, name, price, valid_from, valid_to, operation)
        VALUES (OLD.id, OLD.name, OLD.price, OLD.updated_at, CURRENT_TIMESTAMP, 'D');
        RETURN OLD;
    ELSIF TG_OP = 'INSERT' THEN
        INSERT INTO products_history (id, name, price, valid_from, valid_to, operation)
        VALUES (NEW.id, NEW.name, NEW.price, CURRENT_TIMESTAMP, '9999-12-31', 'I');
        RETURN NEW;
    END IF;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER products_audit
    AFTER INSERT OR UPDATE OR DELETE ON products
    FOR EACH ROW EXECUTE FUNCTION products_audit_trigger();
```

### Point-in-Time Query

```sql
-- Get product state at a specific time
SELECT * FROM products_history
WHERE id = :product_id
  AND :query_time >= valid_from
  AND :query_time < valid_to;
```

### Slowly Changing Dimensions (SCD Type 2)

```sql
CREATE TABLE dim_customer (
    customer_key SERIAL PRIMARY KEY,  -- Surrogate key
    customer_id INTEGER NOT NULL,      -- Natural key

    -- Attributes
    name VARCHAR(100),
    email VARCHAR(255),
    segment VARCHAR(50),

    -- SCD Type 2 fields
    effective_date DATE NOT NULL,
    expiration_date DATE DEFAULT '9999-12-31',
    is_current BOOLEAN DEFAULT TRUE,

    version INTEGER DEFAULT 1
);

-- Unique constraint on natural key + effective date
CREATE UNIQUE INDEX idx_dim_customer_natural
    ON dim_customer(customer_id, effective_date);

-- Fast lookup of current record
CREATE INDEX idx_dim_customer_current
    ON dim_customer(customer_id) WHERE is_current = TRUE;
```

---

## 13. Concurrency Control: Optimistic vs Pessimistic Locking

### Optimistic Locking (Version Column)

```sql
CREATE TABLE inventory (
    id SERIAL PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL,
    version INTEGER NOT NULL DEFAULT 1  -- Version column
);

-- Read the record
SELECT id, quantity, version FROM inventory WHERE product_id = :pid;
-- Returns: {id: 1, quantity: 100, version: 5}

-- Update with version check
UPDATE inventory
SET quantity = quantity - 10,
    version = version + 1
WHERE product_id = :pid
  AND version = 5;  -- Expected version

-- If affected rows = 0, someone else modified it - retry!
```

**Application Code Pattern:**

```python
def decrement_inventory(product_id: int, amount: int, max_retries: int = 3):
    for attempt in range(max_retries):
        # Read current state
        row = db.query("SELECT quantity, version FROM inventory WHERE product_id = %s", product_id)

        if row.quantity < amount:
            raise InsufficientInventoryError()

        # Attempt update with version check
        affected = db.execute("""
            UPDATE inventory
            SET quantity = quantity - %s, version = version + 1
            WHERE product_id = %s AND version = %s
        """, amount, product_id, row.version)

        if affected > 0:
            return  # Success

        # Conflict - retry
        time.sleep(0.1 * (2 ** attempt))  # Exponential backoff

    raise ConcurrencyError("Max retries exceeded")
```

### Pessimistic Locking (SELECT FOR UPDATE)

```sql
-- Acquire exclusive lock on row
BEGIN;

SELECT * FROM inventory
WHERE product_id = :pid
FOR UPDATE;  -- Blocks other transactions

-- Safe to update - we have the lock
UPDATE inventory
SET quantity = quantity - 10
WHERE product_id = :pid;

COMMIT;  -- Releases lock
```

**Lock Modes:**
```sql
-- Exclusive lock (blocks all other locks)
SELECT * FROM inventory WHERE id = 1 FOR UPDATE;

-- Share lock (allows other reads, blocks writes)
SELECT * FROM inventory WHERE id = 1 FOR SHARE;

-- No wait (fail immediately if locked)
SELECT * FROM inventory WHERE id = 1 FOR UPDATE NOWAIT;

-- Skip locked rows (useful for job queues)
SELECT * FROM jobs WHERE status = 'pending'
ORDER BY created_at
LIMIT 10
FOR UPDATE SKIP LOCKED;
```

### When to Use Each

| Scenario | Recommended Approach |
|----------|---------------------|
| Low contention, read-heavy | Optimistic |
| High contention, write-heavy | Pessimistic |
| Long-running transactions | Optimistic |
| Short transactions, critical sections | Pessimistic |
| Distributed systems | Optimistic |
| Financial transactions | Pessimistic |
| Job queues | Pessimistic with SKIP LOCKED |

### Deadlock Prevention

```sql
-- Always lock tables/rows in consistent order
BEGIN;
SELECT * FROM accounts WHERE id = 1 FOR UPDATE;
SELECT * FROM accounts WHERE id = 2 FOR UPDATE;  -- Always lock lower ID first

-- Transfer money
UPDATE accounts SET balance = balance - 100 WHERE id = 1;
UPDATE accounts SET balance = balance + 100 WHERE id = 2;
COMMIT;
```

---

## 14. Database Indexing Strategies

### When to Create Indexes

| Scenario | Index? | Reason |
|----------|--------|--------|
| Columns in WHERE clauses | Yes | Filter optimization |
| Columns in JOIN conditions | Yes | Join performance |
| Columns in ORDER BY | Yes | Avoid sorting |
| Foreign key columns | Yes | FK lookup performance |
| High cardinality columns | Yes | Good selectivity |
| Low cardinality columns (boolean, status) | Maybe | Partial index might help |
| Frequently updated columns | Caution | Index maintenance overhead |
| Large text columns | No | Use full-text search instead |

### Index Types

```sql
-- B-Tree (default, most common)
CREATE INDEX idx_users_email ON users(email);

-- Hash (equality lookups only)
CREATE INDEX idx_users_email_hash ON users USING HASH (email);

-- GIN (arrays, full-text, JSONB)
CREATE INDEX idx_posts_tags ON posts USING GIN (tags);
CREATE INDEX idx_users_metadata ON users USING GIN (metadata jsonb_path_ops);

-- GiST (geometric, range types)
CREATE INDEX idx_events_during ON events USING GIST (tsrange(start_time, end_time));

-- BRIN (large sequential data)
CREATE INDEX idx_logs_created ON logs USING BRIN (created_at);
```

### Composite Indexes

```sql
-- Order matters! Left-to-right
CREATE INDEX idx_orders_customer_date ON orders(customer_id, created_at);

-- This index supports:
WHERE customer_id = 123                          -- Yes
WHERE customer_id = 123 AND created_at > '2024'  -- Yes
WHERE created_at > '2024'                        -- No (can't use index)

-- Rule: Filter columns first, then range/sort columns
CREATE INDEX idx_orders_lookup
    ON orders(customer_id, status, created_at DESC);
```

### Partial Indexes

```sql
-- Index only active records
CREATE INDEX idx_users_email_active
    ON users(email)
    WHERE deleted_at IS NULL;

-- Index only specific status
CREATE INDEX idx_orders_pending
    ON orders(created_at)
    WHERE status = 'pending';
```

### Covering Indexes

```sql
-- Include all needed columns to avoid table lookup
CREATE INDEX idx_orders_covering
    ON orders(customer_id)
    INCLUDE (order_date, total_amount, status);

-- This query uses index-only scan
SELECT order_date, total_amount, status
FROM orders
WHERE customer_id = 123;
```

### Index Anti-Patterns

```sql
-- ANTI-PATTERN: Too many indexes
-- Every index slows down writes
CREATE INDEX idx1 ON users(email);
CREATE INDEX idx2 ON users(email, name);  -- Redundant if idx1 exists for email-only queries
CREATE INDEX idx3 ON users(name, email);  -- Different, but do you need it?

-- ANTI-PATTERN: Indexing low-selectivity columns
CREATE INDEX idx_users_gender ON users(gender);  -- Only 2-3 values, rarely useful

-- ANTI-PATTERN: Not indexing foreign keys
-- FK lookups will be slow without this
ALTER TABLE orders ADD CONSTRAINT fk_customer
    FOREIGN KEY (customer_id) REFERENCES customers(id);
-- Don't forget:
CREATE INDEX idx_orders_customer ON orders(customer_id);
```

### Index Maintenance

```sql
-- Check index usage (PostgreSQL)
SELECT
    schemaname,
    relname as table_name,
    indexrelname as index_name,
    idx_scan as times_used,
    pg_size_pretty(pg_relation_size(indexrelid)) as index_size
FROM pg_stat_user_indexes
ORDER BY idx_scan ASC;

-- Find unused indexes
SELECT * FROM pg_stat_user_indexes
WHERE idx_scan = 0
AND indexrelname NOT LIKE '%_pkey';

-- Rebuild bloated indexes
REINDEX INDEX idx_users_email;
```

---

## 15. NULL Handling & Three-Valued Logic

### The Three Values: TRUE, FALSE, UNKNOWN

SQL uses three-valued logic (3VL). Any comparison with NULL yields UNKNOWN, not TRUE or FALSE.

```sql
-- All of these return UNKNOWN, not TRUE or FALSE
SELECT NULL = NULL;      -- NULL (UNKNOWN)
SELECT NULL = 1;         -- NULL (UNKNOWN)
SELECT NULL > 5;         -- NULL (UNKNOWN)
SELECT NULL <> NULL;     -- NULL (UNKNOWN)
```

### Common Pitfalls

#### Pitfall 1: WHERE Clause Filtering

```sql
-- Records with NULL values are excluded!
SELECT * FROM users WHERE status = 'active';    -- Excludes NULL status
SELECT * FROM users WHERE status <> 'active';   -- ALSO excludes NULL status!

-- To include NULLs:
SELECT * FROM users WHERE status <> 'active' OR status IS NULL;
SELECT * FROM users WHERE COALESCE(status, '') <> 'active';
```

#### Pitfall 2: NOT IN with NULLs

```sql
-- DANGEROUS: If subquery contains NULL, returns no rows!
SELECT * FROM orders
WHERE customer_id NOT IN (SELECT id FROM blacklisted_customers);

-- If blacklisted_customers has a NULL id, this returns NOTHING

-- Safe alternatives:
SELECT * FROM orders
WHERE customer_id NOT IN (
    SELECT id FROM blacklisted_customers WHERE id IS NOT NULL
);

-- Or use NOT EXISTS (recommended):
SELECT * FROM orders o
WHERE NOT EXISTS (
    SELECT 1 FROM blacklisted_customers b WHERE b.id = o.customer_id
);
```

#### Pitfall 3: Aggregates and NULLs

```sql
-- NULLs are ignored by aggregate functions (except COUNT(*))
SELECT AVG(score) FROM tests;  -- NULLs excluded from average
SELECT COUNT(score) FROM tests;  -- Counts non-NULL values only
SELECT COUNT(*) FROM tests;      -- Counts all rows including NULLs

-- This can cause unexpected results:
SELECT
    SUM(amount) / COUNT(*)  -- Includes all rows
    vs
    AVG(amount)             -- Excludes NULLs - different result!
```

#### Pitfall 4: JOINs with NULLs

```sql
-- NULLs don't match in joins
SELECT * FROM orders o
JOIN customers c ON o.customer_id = c.id;
-- Orders with NULL customer_id are excluded

-- To include them:
SELECT * FROM orders o
LEFT JOIN customers c ON o.customer_id = c.id;
```

### Best Practices

```sql
-- Use IS NULL / IS NOT NULL, never = NULL
WHERE email IS NULL;        -- Correct
WHERE email = NULL;         -- Always returns no rows!

-- Use COALESCE for default values
SELECT COALESCE(nickname, first_name, 'Anonymous') as display_name;

-- Use NULLIF to create NULLs
SELECT amount / NULLIF(count, 0);  -- Avoid division by zero

-- Document NULL semantics
-- NULL in price means "price not yet set"
-- NULL in deleted_at means "not deleted"

-- Consider NOT NULL constraints
CREATE TABLE orders (
    id SERIAL PRIMARY KEY,
    customer_id INTEGER NOT NULL,  -- Require a customer
    notes TEXT  -- Optional, NULL allowed
);
```

---

## 16. Sharding & Partitioning

### Partitioning (Single Database)

```sql
-- Range partitioning by date (PostgreSQL)
CREATE TABLE events (
    id SERIAL,
    event_type VARCHAR(50),
    created_at TIMESTAMP NOT NULL,
    data JSONB
) PARTITION BY RANGE (created_at);

-- Create partitions
CREATE TABLE events_2024_q1 PARTITION OF events
    FOR VALUES FROM ('2024-01-01') TO ('2024-04-01');

CREATE TABLE events_2024_q2 PARTITION OF events
    FOR VALUES FROM ('2024-04-01') TO ('2024-07-01');

-- Queries automatically route to correct partition
SELECT * FROM events WHERE created_at >= '2024-02-01';
```

### List Partitioning

```sql
CREATE TABLE orders (
    id SERIAL,
    region VARCHAR(20) NOT NULL,
    amount DECIMAL(10,2)
) PARTITION BY LIST (region);

CREATE TABLE orders_americas PARTITION OF orders
    FOR VALUES IN ('US', 'CA', 'MX', 'BR');

CREATE TABLE orders_europe PARTITION OF orders
    FOR VALUES IN ('UK', 'DE', 'FR', 'ES');

CREATE TABLE orders_apac PARTITION OF orders
    FOR VALUES IN ('JP', 'CN', 'AU', 'IN');
```

### Hash Partitioning

```sql
CREATE TABLE sessions (
    id UUID PRIMARY KEY,
    user_id INTEGER NOT NULL,
    data JSONB
) PARTITION BY HASH (user_id);

CREATE TABLE sessions_p0 PARTITION OF sessions
    FOR VALUES WITH (MODULUS 4, REMAINDER 0);
CREATE TABLE sessions_p1 PARTITION OF sessions
    FOR VALUES WITH (MODULUS 4, REMAINDER 1);
CREATE TABLE sessions_p2 PARTITION OF sessions
    FOR VALUES WITH (MODULUS 4, REMAINDER 2);
CREATE TABLE sessions_p3 PARTITION OF sessions
    FOR VALUES WITH (MODULUS 4, REMAINDER 3);
```

### Sharding (Distributed Databases)

Sharding distributes data across multiple database servers.

#### Shard Key Selection (Critical)

| Shard Key | Pros | Cons |
|-----------|------|------|
| user_id | Even distribution | Cross-user queries need scatter-gather |
| tenant_id | Tenant isolation | Uneven if tenant sizes vary |
| geographic region | Data locality | Uneven distribution |
| hash(id) | Even distribution | No locality, range queries expensive |

```sql
-- Application-level sharding logic
def get_shard(user_id: int) -> str:
    shard_count = 16
    shard_index = user_id % shard_count
    return f"shard_{shard_index}"

# All queries for a user go to the same shard
shard = get_shard(user_id)
db = connect(shard)
db.query("SELECT * FROM orders WHERE user_id = %s", user_id)
```

### When to Use Each

| Data Size | Approach |
|-----------|----------|
| < 100GB | No partitioning needed |
| 100GB - 1TB | Consider partitioning |
| > 1TB | Partitioning required |
| > 10TB single table | Consider sharding |

---

## 17. Common Anti-Patterns to Avoid

### 1. Missing Primary Keys

```sql
-- ANTI-PATTERN
CREATE TABLE logs (
    timestamp TIMESTAMP,
    message TEXT
);

-- CORRECT
CREATE TABLE logs (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMP NOT NULL,
    message TEXT NOT NULL
);
```

### 2. Entity-Attribute-Value (EAV)

```sql
-- ANTI-PATTERN: Generic key-value table
CREATE TABLE entity_attributes (
    entity_id INTEGER,
    attribute_name VARCHAR(100),
    attribute_value TEXT
);

-- CORRECT: Proper schema with typed columns
CREATE TABLE products (
    id SERIAL PRIMARY KEY,
    name VARCHAR(200),
    price DECIMAL(10,2),
    weight_kg DECIMAL(5,2),
    color VARCHAR(50)
);
```

### 3. CSV in Columns

```sql
-- ANTI-PATTERN: Storing lists as delimited strings
CREATE TABLE posts (
    id SERIAL PRIMARY KEY,
    tags VARCHAR(500)  -- "tech,programming,database"
);

-- CORRECT: Proper many-to-many
CREATE TABLE post_tags (
    post_id INTEGER REFERENCES posts(id),
    tag_id INTEGER REFERENCES tags(id),
    PRIMARY KEY (post_id, tag_id)
);

-- Or use arrays (PostgreSQL)
CREATE TABLE posts (
    id SERIAL PRIMARY KEY,
    tags TEXT[] NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_posts_tags ON posts USING GIN (tags);
```

### 4. God Table

```sql
-- ANTI-PATTERN: One table for everything
CREATE TABLE master_data (
    id SERIAL PRIMARY KEY,
    type VARCHAR(50),  -- 'user', 'product', 'order'
    name VARCHAR(200),
    email VARCHAR(255),
    price DECIMAL(10,2),
    quantity INTEGER,
    -- ... 100 more columns
);

-- CORRECT: Separate tables for separate entities
```

### 5. Over-Reliance on SELECT *

```sql
-- ANTI-PATTERN
SELECT * FROM orders WHERE customer_id = 123;

-- CORRECT: Select only needed columns
SELECT id, order_date, total_amount
FROM orders
WHERE customer_id = 123;
```

### 6. Missing Foreign Key Indexes

```sql
-- Creating FK without index (common oversight)
ALTER TABLE orders
ADD CONSTRAINT fk_customer
FOREIGN KEY (customer_id) REFERENCES customers(id);

-- Don't forget the index!
CREATE INDEX idx_orders_customer ON orders(customer_id);
```

### 7. Using ENUM for Changing Values

```sql
-- ANTI-PATTERN: ENUM that will need changes
CREATE TYPE order_status AS ENUM ('pending', 'confirmed', 'shipped');
-- Adding new values requires ALTER TYPE which can be problematic

-- BETTER: VARCHAR with check constraint or lookup table
CREATE TABLE order_statuses (
    code VARCHAR(20) PRIMARY KEY,
    name VARCHAR(50) NOT NULL
);
```

### 8. Deep Nested Views

```sql
-- ANTI-PATTERN: Views referencing views referencing views
CREATE VIEW v1 AS SELECT * FROM base_table WHERE x = 1;
CREATE VIEW v2 AS SELECT * FROM v1 WHERE y = 2;
CREATE VIEW v3 AS SELECT * FROM v2 WHERE z = 3;
CREATE VIEW v4 AS SELECT * FROM v3 WHERE w = 4;
-- Optimizer struggles, humans struggle, everyone struggles

-- CORRECT: Flatten or use materialized views
```

---

## 17B. SQL Antipatterns: Bill Karwin's Complete Reference

This section provides a comprehensive catalog of SQL antipatterns from Bill Karwin's seminal book ["SQL Antipatterns: Avoiding the Pitfalls of Database Programming"](https://pragprog.com/titles/bksqla/sql-antipatterns/). Each antipattern follows the format: **Objective** (what you're trying to achieve), **Antipattern** (the common mistake), and **Solution** (the correct approach).

---

### Part I: Logical Database Design Antipatterns

#### 1. Jaywalking

**Objective:** Store multivalue attributes (e.g., tags, categories, permissions)

**Antipattern:** Store comma-separated lists in a VARCHAR column

```sql
-- ANTI-PATTERN: Comma-separated values
CREATE TABLE products (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100),
    tags VARCHAR(500)  -- "electronics,gadget,sale"
);

-- Querying is painful and inefficient
SELECT * FROM products WHERE tags LIKE '%gadget%';  -- Can't use indexes properly
```

**Problems:**
- Cannot use indexes efficiently
- Cannot enforce referential integrity
- Cannot easily count, sort, or aggregate values
- Maximum length limits the number of values
- Separator character might appear in data

**Solution:** Create an intersection table

```sql
-- CORRECT: Intersection table
CREATE TABLE products (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100)
);

CREATE TABLE tags (
    id SERIAL PRIMARY KEY,
    name VARCHAR(50) UNIQUE
);

CREATE TABLE product_tags (
    product_id INTEGER REFERENCES products(id) ON DELETE CASCADE,
    tag_id INTEGER REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (product_id, tag_id)
);

-- Now queries are efficient and data is validated
SELECT p.* FROM products p
JOIN product_tags pt ON p.id = pt.product_id
JOIN tags t ON pt.tag_id = t.id
WHERE t.name = 'gadget';
```

---

#### 2. Naive Trees

**Objective:** Store and query hierarchical data (org charts, categories, threaded comments)

**Antipattern:** Use adjacency list with only `parent_id` column

```sql
-- ANTI-PATTERN: Simple adjacency list
CREATE TABLE comments (
    id SERIAL PRIMARY KEY,
    parent_id INTEGER REFERENCES comments(id),
    content TEXT
);

-- Getting entire tree requires recursive queries or multiple round-trips
-- Deleting a node requires handling all descendants
```

**Problems:**
- Retrieving an entire subtree requires recursive CTEs or multiple queries
- Counting descendants is expensive
- Deleting/moving subtrees is complex

**Solutions:**

**A. Path Enumeration** - Store the path from root to each node

```sql
CREATE TABLE comments (
    id SERIAL PRIMARY KEY,
    path VARCHAR(1000),  -- '/1/4/6/7/'
    content TEXT
);

-- Get all ancestors
SELECT * FROM comments WHERE '/1/4/6/7/' LIKE path || '%';

-- Get all descendants
SELECT * FROM comments WHERE path LIKE '/1/4/%';
```

**B. Nested Sets** - Store left/right boundary numbers

```sql
CREATE TABLE categories (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100),
    nsleft INTEGER NOT NULL,
    nsright INTEGER NOT NULL
);

-- Get all descendants (fast!)
SELECT * FROM categories WHERE nsleft BETWEEN 2 AND 9;

-- Get depth
SELECT c.*, (COUNT(parent.id) - 1) AS depth
FROM categories c, categories parent
WHERE c.nsleft BETWEEN parent.nsleft AND parent.nsright
GROUP BY c.id;
```

**C. Closure Table** - Store all ancestor-descendant relationships

```sql
CREATE TABLE comments (
    id SERIAL PRIMARY KEY,
    content TEXT
);

CREATE TABLE comment_tree (
    ancestor_id INTEGER REFERENCES comments(id),
    descendant_id INTEGER REFERENCES comments(id),
    depth INTEGER NOT NULL,
    PRIMARY KEY (ancestor_id, descendant_id)
);

-- Get all descendants
SELECT c.* FROM comments c
JOIN comment_tree t ON c.id = t.descendant_id
WHERE t.ancestor_id = 4;

-- Get all ancestors
SELECT c.* FROM comments c
JOIN comment_tree t ON c.id = t.ancestor_id
WHERE t.descendant_id = 7;
```

| Approach | Query Subtree | Query Ancestors | Insert | Delete | Move Subtree |
|----------|---------------|-----------------|--------|--------|--------------|
| Adjacency List | Hard | Hard | Easy | Medium | Easy |
| Path Enumeration | Easy | Easy | Easy | Easy | Hard |
| Nested Sets | Easy | Easy | Hard | Hard | Hard |
| Closure Table | Easy | Easy | Easy | Easy | Easy |

---

#### 3. ID Required

**Objective:** Establish primary key conventions

**Antipattern:** Blindly add `id` column to every table

```sql
-- ANTI-PATTERN: Redundant surrogate key
CREATE TABLE product_tags (
    id SERIAL PRIMARY KEY,  -- Unnecessary!
    product_id INTEGER REFERENCES products(id),
    tag_id INTEGER REFERENCES tags(id)
);

-- Now you need a unique constraint anyway
ALTER TABLE product_tags ADD UNIQUE (product_id, tag_id);
```

**Problems:**
- Allows duplicate relationships
- Wastes storage
- Adds unnecessary column
- May encourage incorrect joins

**Solution:** Use natural or compound keys when appropriate

```sql
-- CORRECT: Compound primary key
CREATE TABLE product_tags (
    product_id INTEGER REFERENCES products(id),
    tag_id INTEGER REFERENCES tags(id),
    PRIMARY KEY (product_id, tag_id)  -- Natural compound key
);
```

**When surrogate keys ARE appropriate:**
- When natural key is too wide (multiple columns, long strings)
- When natural key values change frequently
- When you need to hide business information
- When following ORM conventions

---

#### 4. Keyless Entry

**Objective:** Simplify database development

**Antipattern:** Skip foreign key constraints

```sql
-- ANTI-PATTERN: No foreign key constraint
CREATE TABLE orders (
    id SERIAL PRIMARY KEY,
    customer_id INTEGER,  -- No FK constraint!
    total DECIMAL(10,2)
);

-- Application can insert invalid customer_id
INSERT INTO orders (customer_id, total) VALUES (99999, 100.00);  -- No error!
```

**Problems:**
- Orphaned rows (orders without valid customers)
- Data corruption from application bugs
- No cascading deletes/updates
- Must enforce integrity in every application accessing the data

**Solution:** Always declare foreign key constraints

```sql
-- CORRECT: With foreign key constraints
CREATE TABLE orders (
    id SERIAL PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id)
        ON DELETE RESTRICT
        ON UPDATE CASCADE,
    total DECIMAL(10,2)
);

-- Don't forget to index the foreign key!
CREATE INDEX idx_orders_customer ON orders(customer_id);
```

---

#### 5. Entity-Attribute-Value (EAV)

**Objective:** Support variable/dynamic attributes

**Antipattern:** Use a generic attribute table

```sql
-- ANTI-PATTERN: EAV table
CREATE TABLE entity_attributes (
    entity_id INTEGER,
    entity_type VARCHAR(50),
    attribute_name VARCHAR(100),
    attribute_value TEXT,  -- Everything becomes text!
    PRIMARY KEY (entity_id, entity_type, attribute_name)
);

-- Querying is a nightmare
SELECT
    e.entity_id,
    MAX(CASE WHEN attribute_name = 'color' THEN attribute_value END) as color,
    MAX(CASE WHEN attribute_name = 'size' THEN attribute_value END) as size,
    MAX(CASE WHEN attribute_name = 'weight' THEN attribute_value END) as weight
FROM entity_attributes e
WHERE entity_type = 'product'
GROUP BY e.entity_id;
```

**Problems:**
- Cannot enforce data types (everything is TEXT)
- Cannot enforce NOT NULL or other constraints
- Cannot use foreign key constraints
- Queries become complex pivots
- Poor query performance
- Cannot enforce attribute names (typos create new "attributes")

**Solutions:**

**A. Single Table Inheritance** - One table with all possible columns

```sql
CREATE TABLE products (
    id SERIAL PRIMARY KEY,
    type VARCHAR(50) NOT NULL,
    name VARCHAR(100) NOT NULL,
    -- Common attributes
    price DECIMAL(10,2) NOT NULL,
    -- Electronics attributes
    voltage INTEGER,
    warranty_months INTEGER,
    -- Clothing attributes
    size VARCHAR(10),
    color VARCHAR(50),
    material VARCHAR(100)
);
```

**B. Class Table Inheritance** - Base table plus type-specific tables

```sql
CREATE TABLE products (
    id SERIAL PRIMARY KEY,
    type VARCHAR(50) NOT NULL,
    name VARCHAR(100) NOT NULL,
    price DECIMAL(10,2) NOT NULL
);

CREATE TABLE electronics (
    product_id INTEGER PRIMARY KEY REFERENCES products(id),
    voltage INTEGER,
    warranty_months INTEGER
);

CREATE TABLE clothing (
    product_id INTEGER PRIMARY KEY REFERENCES products(id),
    size VARCHAR(10),
    color VARCHAR(50),
    material VARCHAR(100)
);
```

**C. Semi-Structured Data** - Use JSON/JSONB for truly dynamic attributes

```sql
CREATE TABLE products (
    id SERIAL PRIMARY KEY,
    type VARCHAR(50) NOT NULL,
    name VARCHAR(100) NOT NULL,
    price DECIMAL(10,2) NOT NULL,
    attributes JSONB DEFAULT '{}'
);

-- Query JSON attributes
SELECT * FROM products
WHERE attributes->>'color' = 'red'
  AND (attributes->>'size')::int > 10;
```

---

#### 6. Polymorphic Associations

**Objective:** Reference multiple parent tables from one child table

**Antipattern:** Use a dual-purpose foreign key with type column

```sql
-- ANTI-PATTERN: Polymorphic association
CREATE TABLE comments (
    id SERIAL PRIMARY KEY,
    commentable_type VARCHAR(50),  -- 'Article', 'Photo', 'Video'
    commentable_id INTEGER,        -- Can't have FK constraint!
    content TEXT
);

-- No referential integrity - this is NOT a real foreign key
```

**Problems:**
- Cannot define foreign key constraint
- Cannot use JOIN directly
- Must use UNION or conditional logic
- No cascading deletes
- Type column can have invalid values

**Solutions:**

**A. Exclusive Belongs-To (Nullable FKs)**

```sql
CREATE TABLE comments (
    id SERIAL PRIMARY KEY,
    article_id INTEGER REFERENCES articles(id),
    photo_id INTEGER REFERENCES photos(id),
    video_id INTEGER REFERENCES videos(id),
    content TEXT,
    -- Ensure exactly one parent
    CONSTRAINT one_parent CHECK (
        (article_id IS NOT NULL)::int +
        (photo_id IS NOT NULL)::int +
        (video_id IS NOT NULL)::int = 1
    )
);
```

**B. Common Super-Table**

```sql
CREATE TABLE commentables (
    id SERIAL PRIMARY KEY,
    type VARCHAR(50) NOT NULL
);

CREATE TABLE articles (
    commentable_id INTEGER PRIMARY KEY REFERENCES commentables(id),
    title VARCHAR(200),
    body TEXT
);

CREATE TABLE photos (
    commentable_id INTEGER PRIMARY KEY REFERENCES commentables(id),
    url VARCHAR(500),
    caption TEXT
);

CREATE TABLE comments (
    id SERIAL PRIMARY KEY,
    commentable_id INTEGER REFERENCES commentables(id),  -- Real FK!
    content TEXT
);
```

---

#### 7. Multicolumn Attributes

**Objective:** Store multiple values of the same attribute

**Antipattern:** Create numbered columns

```sql
-- ANTI-PATTERN: Multiple columns for same attribute
CREATE TABLE contacts (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100),
    phone1 VARCHAR(20),
    phone2 VARCHAR(20),
    phone3 VARCHAR(20)  -- What if they have 4 phones?
);

-- Querying any phone is awkward
SELECT * FROM contacts
WHERE phone1 = '555-1234' OR phone2 = '555-1234' OR phone3 = '555-1234';
```

**Problems:**
- Fixed maximum number of values
- Searching requires checking all columns
- Adding more requires schema change
- Sparse data (most rows use 1-2 phones)

**Solution:** Create a dependent table

```sql
-- CORRECT: Dependent table
CREATE TABLE contacts (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100)
);

CREATE TABLE contact_phones (
    id SERIAL PRIMARY KEY,
    contact_id INTEGER REFERENCES contacts(id) ON DELETE CASCADE,
    phone VARCHAR(20) NOT NULL,
    type VARCHAR(20) DEFAULT 'mobile',  -- 'mobile', 'home', 'work'
    is_primary BOOLEAN DEFAULT false
);

-- Easy to query
SELECT c.* FROM contacts c
JOIN contact_phones p ON c.id = p.contact_id
WHERE p.phone = '555-1234';
```

---

#### 8. Metadata Tribbles

**Objective:** Support data scalability

**Antipattern:** Clone tables or columns based on data values

```sql
-- ANTI-PATTERN: Table per year
CREATE TABLE sales_2023 (...);
CREATE TABLE sales_2024 (...);
CREATE TABLE sales_2025 (...);

-- Or columns per value
CREATE TABLE metrics (
    id SERIAL PRIMARY KEY,
    jan_value DECIMAL,
    feb_value DECIMAL,
    mar_value DECIMAL,
    -- ... 12 columns total
);
```

**Problems:**
- Queries across time periods require UNION ALL
- New time periods require schema changes
- Constraints must be duplicated
- Indexes must be duplicated
- Reports and aggregations are complex

**Solution:** Use database-managed partitioning

```sql
-- CORRECT: Native partitioning (PostgreSQL)
CREATE TABLE sales (
    id SERIAL,
    sale_date DATE NOT NULL,
    amount DECIMAL(10,2),
    customer_id INTEGER
) PARTITION BY RANGE (sale_date);

CREATE TABLE sales_2024 PARTITION OF sales
    FOR VALUES FROM ('2024-01-01') TO ('2025-01-01');

CREATE TABLE sales_2025 PARTITION OF sales
    FOR VALUES FROM ('2025-01-01') TO ('2026-01-01');

-- Query as single table - optimizer handles partition pruning
SELECT SUM(amount) FROM sales WHERE sale_date >= '2024-06-01';
```

---

### Part II: Physical Database Design Antipatterns

#### 9. Rounding Errors

**Objective:** Store fractional numeric data

**Antipattern:** Use FLOAT or DOUBLE for currency/financial data

```sql
-- ANTI-PATTERN: Using FLOAT for money
CREATE TABLE accounts (
    id SERIAL PRIMARY KEY,
    balance FLOAT  -- Dangerous!
);

-- Rounding errors accumulate
INSERT INTO accounts (balance) VALUES (0.1 + 0.2);  -- Not exactly 0.3!
```

**Problems:**
- FLOAT uses binary representation (base-2)
- Cannot exactly represent many decimal fractions (0.1, 0.2, etc.)
- Rounding errors compound over time
- Comparisons may fail unexpectedly

**Solution:** Use DECIMAL/NUMERIC for exact precision

```sql
-- CORRECT: DECIMAL for exact arithmetic
CREATE TABLE accounts (
    id SERIAL PRIMARY KEY,
    balance DECIMAL(15,2) NOT NULL DEFAULT 0.00  -- Up to 15 digits, 2 decimal places
);

-- Or use smallest currency unit (cents)
CREATE TABLE accounts (
    id SERIAL PRIMARY KEY,
    balance_cents BIGINT NOT NULL DEFAULT 0
);
```

---

#### 10. 31 Flavors

**Objective:** Restrict a column to a set of valid values

**Antipattern:** Use ENUM or CHECK constraints with hardcoded values

```sql
-- ANTI-PATTERN: ENUM with values that will change
CREATE TYPE bug_status AS ENUM ('new', 'open', 'fixed');

-- Adding 'verified' requires ALTER TYPE - problematic in many databases
-- Some databases don't support removing ENUM values at all
```

**Problems:**
- Adding/removing values requires schema change
- ENUM behavior varies across databases
- Cannot query the list of valid values
- Cannot add metadata to values (display order, description)

**Solution:** Use a lookup table

```sql
-- CORRECT: Lookup table
CREATE TABLE bug_statuses (
    status VARCHAR(20) PRIMARY KEY,
    description TEXT,
    display_order INTEGER,
    is_active BOOLEAN DEFAULT true
);

INSERT INTO bug_statuses VALUES
    ('new', 'Newly reported', 1, true),
    ('open', 'Under investigation', 2, true),
    ('fixed', 'Fix implemented', 3, true),
    ('verified', 'Fix verified', 4, true);

CREATE TABLE bugs (
    id SERIAL PRIMARY KEY,
    status VARCHAR(20) REFERENCES bug_statuses(status) DEFAULT 'new'
);
```

---

#### 11. Phantom Files

**Objective:** Store images, documents, or other files

**Antipattern:** Store only file paths in database

```sql
-- ANTI-PATTERN: Path to external file
CREATE TABLE documents (
    id SERIAL PRIMARY KEY,
    file_path VARCHAR(500),  -- '/uploads/doc_123.pdf'
    uploaded_at TIMESTAMP
);
```

**Problems:**
- Files can be deleted outside database transaction
- Database backup doesn't include files
- File permissions are separate from database permissions
- Orphaned files when database rows are deleted
- No transactional consistency

**Solutions:**

**A. Store files in database (BLOB)**

```sql
CREATE TABLE documents (
    id SERIAL PRIMARY KEY,
    file_name VARCHAR(255),
    mime_type VARCHAR(100),
    content BYTEA,  -- PostgreSQL; use BLOB for MySQL
    uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**B. Use object storage with proper integration**

```sql
-- Track external storage with proper lifecycle management
CREATE TABLE documents (
    id SERIAL PRIMARY KEY,
    storage_bucket VARCHAR(100) NOT NULL,
    storage_key VARCHAR(500) NOT NULL,  -- S3 key or similar
    file_name VARCHAR(255),
    mime_type VARCHAR(100),
    file_size BIGINT,
    checksum VARCHAR(64),  -- SHA-256 for integrity
    uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (storage_bucket, storage_key)
);
-- Use application logic to ensure files are deleted with records
```

**When to use each:**
- BLOB: Small files (<10MB), need ACID, limited concurrent access
- External storage: Large files, high concurrency, CDN integration needed

---

#### 12. Index Shotgun

**Objective:** Optimize query performance

**Antipattern:** Create indexes without analysis, or create none at all

```sql
-- ANTI-PATTERN: Index everything blindly
CREATE INDEX idx1 ON orders(customer_id);
CREATE INDEX idx2 ON orders(order_date);
CREATE INDEX idx3 ON orders(status);
CREATE INDEX idx4 ON orders(customer_id, order_date);
CREATE INDEX idx5 ON orders(customer_id, status);
CREATE INDEX idx6 ON orders(order_date, status);
-- Slows down all writes, wastes storage
```

**Problems:**
- Too few indexes: slow queries
- Too many indexes: slow writes, wasted storage
- Wrong indexes: indexes that are never used
- Missing covering indexes: unnecessary table lookups

**Solution:** Analyze query patterns, then create targeted indexes

```sql
-- 1. Analyze slow queries with EXPLAIN ANALYZE
EXPLAIN ANALYZE
SELECT * FROM orders WHERE customer_id = 123 AND status = 'pending';

-- 2. Check existing index usage
SELECT indexrelname, idx_scan, idx_tup_read, idx_tup_fetch
FROM pg_stat_user_indexes
WHERE schemaname = 'public'
ORDER BY idx_scan;

-- 3. Create indexes based on actual query patterns
-- For: WHERE customer_id = ? AND status = ?
CREATE INDEX idx_orders_customer_status ON orders(customer_id, status);

-- 4. Use covering indexes for frequently accessed columns
CREATE INDEX idx_orders_customer_covering
ON orders(customer_id) INCLUDE (order_date, total);
```

---

### Part III: Query Antipatterns

#### 13. Fear of the Unknown (NULL Handling)

**Objective:** Handle missing or unknown data

**Antipattern:** Treat NULL as a regular value

```sql
-- ANTI-PATTERN: Incorrect NULL comparisons
SELECT * FROM users WHERE phone = NULL;      -- Always returns 0 rows!
SELECT * FROM users WHERE phone <> '555';    -- Excludes NULL phones!

-- Concatenation with NULL
SELECT 'Hello ' || middle_name || '!' FROM users;  -- Returns NULL if middle_name is NULL
```

**Problems:**
- NULL = NULL is NULL (unknown), not TRUE
- NULL in any comparison yields NULL (not TRUE or FALSE)
- NULL propagates through expressions
- COUNT(*) vs COUNT(column) behave differently

**Solution:** Use NULL-safe operators and functions

```sql
-- CORRECT: NULL-safe queries
SELECT * FROM users WHERE phone IS NULL;
SELECT * FROM users WHERE phone IS NOT NULL;

-- Include NULLs in inequality
SELECT * FROM users WHERE phone <> '555' OR phone IS NULL;

-- NULL-safe equality (PostgreSQL/MySQL)
SELECT * FROM users WHERE phone IS NOT DISTINCT FROM other_phone;

-- Handle NULL in concatenation
SELECT 'Hello ' || COALESCE(middle_name, '') || '!' FROM users;

-- Default values
SELECT COALESCE(phone, 'N/A') as phone FROM users;
```

---

#### 14. Ambiguous Groups

**Objective:** Get rows with aggregate values per group

**Antipattern:** Select non-grouped columns without aggregates

```sql
-- ANTI-PATTERN: Which product_name is returned?
SELECT customer_id, MAX(order_date), product_name  -- product_name is ambiguous!
FROM orders
GROUP BY customer_id;

-- Some databases allow this (MySQL with ONLY_FULL_GROUP_BY disabled)
-- Result is unpredictable
```

**Problems:**
- Violates Single-Value Rule (each non-aggregate column must have one value per group)
- Different databases handle this differently
- Results are undefined/unpredictable

**Solutions:**

```sql
-- Solution 1: Correlated subquery
SELECT o1.customer_id, o1.order_date, o1.product_name
FROM orders o1
WHERE o1.order_date = (
    SELECT MAX(o2.order_date)
    FROM orders o2
    WHERE o2.customer_id = o1.customer_id
);

-- Solution 2: Window function
SELECT customer_id, order_date, product_name
FROM (
    SELECT customer_id, order_date, product_name,
           ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY order_date DESC) as rn
    FROM orders
) ranked
WHERE rn = 1;

-- Solution 3: Aggregate all selected columns
SELECT customer_id,
       MAX(order_date) as latest_date,
       STRING_AGG(DISTINCT product_name, ', ') as products  -- Aggregate the column
FROM orders
GROUP BY customer_id;
```

---

#### 15. Random Selection

**Objective:** Select random sample rows

**Antipattern:** Use ORDER BY RAND()

```sql
-- ANTI-PATTERN: Full table scan + sort
SELECT * FROM products ORDER BY RANDOM() LIMIT 1;

-- For 1M rows, this scans and sorts ALL 1M rows to get 1
```

**Problems:**
- Scans entire table
- Sorts all rows (O(n log n))
- Cannot use indexes
- Very slow on large tables

**Solutions:**

```sql
-- Solution 1: Random offset (if IDs are mostly contiguous)
SELECT * FROM products
WHERE id >= (SELECT FLOOR(RANDOM() * (SELECT MAX(id) FROM products)))
ORDER BY id LIMIT 1;

-- Solution 2: TABLESAMPLE (PostgreSQL)
SELECT * FROM products TABLESAMPLE BERNOULLI(0.1) LIMIT 1;

-- Solution 3: Materialized random sample
CREATE MATERIALIZED VIEW random_products AS
SELECT * FROM products ORDER BY RANDOM() LIMIT 1000;
-- Refresh periodically: REFRESH MATERIALIZED VIEW random_products;
```

---

#### 16. Poor Man's Search Engine

**Objective:** Implement keyword search

**Antipattern:** Use LIKE with wildcards

```sql
-- ANTI-PATTERN: Pattern matching for search
SELECT * FROM articles WHERE body LIKE '%database%';

-- Leading wildcard prevents index usage
-- No relevance ranking
-- No stemming (database vs databases)
```

**Problems:**
- Leading wildcard (%) prevents index usage
- No relevance scoring
- No stemming or fuzzy matching
- Case sensitivity issues
- Very slow on large text

**Solution:** Use full-text search

```sql
-- PostgreSQL full-text search
ALTER TABLE articles ADD COLUMN search_vector tsvector;

UPDATE articles SET search_vector =
    to_tsvector('english', COALESCE(title, '') || ' ' || COALESCE(body, ''));

CREATE INDEX idx_articles_search ON articles USING GIN(search_vector);

-- Search with ranking
SELECT title, ts_rank(search_vector, query) as rank
FROM articles, to_tsquery('english', 'database & design') query
WHERE search_vector @@ query
ORDER BY rank DESC;
```

For large-scale search, use dedicated search engines (Elasticsearch, Meilisearch, Typesense).

---

#### 17. Spaghetti Query

**Objective:** Get multiple results in one query

**Antipattern:** Write overly complex single queries

```sql
-- ANTI-PATTERN: Everything in one query
SELECT
    c.name,
    (SELECT COUNT(*) FROM orders WHERE customer_id = c.id) as order_count,
    (SELECT SUM(amount) FROM orders WHERE customer_id = c.id) as total_spent,
    (SELECT MAX(order_date) FROM orders WHERE customer_id = c.id) as last_order,
    (SELECT AVG(rating) FROM reviews WHERE customer_id = c.id) as avg_rating,
    (SELECT COUNT(*) FROM support_tickets WHERE customer_id = c.id) as ticket_count
FROM customers c
WHERE c.status = 'active';
-- Multiple correlated subqueries = multiple table scans
```

**Problems:**
- Hard to read and maintain
- Hard to debug
- Hard to optimize
- Cartesian products from missing join conditions
- Correlated subqueries multiply execution time

**Solution:** Use multiple simpler queries or proper joins

```sql
-- Better: JOINs with aggregation
SELECT
    c.name,
    COUNT(DISTINCT o.id) as order_count,
    SUM(o.amount) as total_spent,
    MAX(o.order_date) as last_order,
    AVG(r.rating) as avg_rating,
    COUNT(DISTINCT t.id) as ticket_count
FROM customers c
LEFT JOIN orders o ON c.id = o.customer_id
LEFT JOIN reviews r ON c.id = r.customer_id
LEFT JOIN support_tickets t ON c.id = t.customer_id
WHERE c.status = 'active'
GROUP BY c.id, c.name;

-- Or: Multiple simple queries (often faster and more maintainable)
-- Query 1: Basic customer info
-- Query 2: Order statistics
-- Query 3: Review statistics
-- Combine in application layer
```

---

#### 18. Implicit Columns

**Objective:** Write concise queries

**Antipattern:** Use SELECT * or omit column names in INSERT

```sql
-- ANTI-PATTERN: SELECT *
SELECT * FROM orders WHERE customer_id = 123;

-- ANTI-PATTERN: INSERT without column names
INSERT INTO users VALUES (1, 'John', 'john@email.com', NOW());
```

**Problems:**
- Schema changes break queries silently
- Fetches unnecessary data (bandwidth, memory)
- INSERT fails if column order changes
- Harder to understand query intent
- Prevents covering index optimization

**Solution:** Always specify columns explicitly

```sql
-- CORRECT: Explicit columns
SELECT id, order_date, total_amount, status
FROM orders
WHERE customer_id = 123;

-- CORRECT: Named INSERT
INSERT INTO users (id, name, email, created_at)
VALUES (1, 'John', 'john@email.com', NOW());
```

---

### Part IV: Application Development Antipatterns

#### 19. Readable Passwords

**Objective:** Store user credentials

**Antipattern:** Store passwords in plain text

```sql
-- ANTI-PATTERN: Plain text passwords
CREATE TABLE users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(50),
    password VARCHAR(100)  -- Storing "mypassword123" directly!
);
```

**Problems:**
- Database breach exposes all passwords
- DBAs and developers can see passwords
- Violates security best practices and regulations
- Users who reuse passwords are vulnerable elsewhere

**Solution:** Hash passwords with salt in application layer

```sql
CREATE TABLE users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(50) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL  -- bcrypt/argon2 hash
);
```

```python
# Application code (Python example)
import bcrypt

# When creating user
password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt())

# When verifying login
if bcrypt.checkpw(password.encode(), stored_hash):
    # Login successful
```

---

#### 20. SQL Injection

**Objective:** Build dynamic queries

**Antipattern:** Concatenate user input into SQL strings

```python
# ANTI-PATTERN: String concatenation
query = f"SELECT * FROM users WHERE username = '{username}' AND password = '{password}'"

# Attacker input: username = "admin'--"
# Resulting query: SELECT * FROM users WHERE username = 'admin'--' AND password = ''
# The -- comments out the password check!
```

**Problems:**
- Attacker can execute arbitrary SQL
- Can read, modify, or delete any data
- Can bypass authentication
- Can potentially access the underlying system

**Solution:** Use parameterized queries

```python
# CORRECT: Parameterized query
cursor.execute(
    "SELECT * FROM users WHERE username = %s AND password_hash = %s",
    (username, password_hash)
)

# Or use an ORM
user = session.query(User).filter(User.username == username).first()
```

---

#### 21. Pseudokey Neat-Freak

**Objective:** Maintain clean primary key sequences

**Antipattern:** Try to reuse deleted IDs or eliminate gaps

```sql
-- ANTI-PATTERN: Finding and filling gaps
INSERT INTO users (id, name)
SELECT MIN(t1.id + 1), 'New User'
FROM users t1
LEFT JOIN users t2 ON t1.id + 1 = t2.id
WHERE t2.id IS NULL;

-- Or resetting sequence after deletes
SELECT setval('users_id_seq', (SELECT MAX(id) FROM users));
```

**Problems:**
- Race conditions in concurrent systems
- Previously deleted IDs may be referenced elsewhere (logs, external systems)
- Expensive queries to find gaps
- No actual benefit

**Solution:** Accept gaps as normal

```sql
-- CORRECT: Let the database manage sequences
INSERT INTO users (name) VALUES ('New User');  -- ID assigned automatically

-- Primary keys are identifiers, not counters
-- Gaps are normal and harmless
```

---

#### 22. See No Evil

**Objective:** Debug database issues

**Antipattern:** Ignore or suppress error messages

```python
# ANTI-PATTERN: Catching and ignoring errors
try:
    cursor.execute(query)
except Exception:
    pass  # Silently fail

# ANTI-PATTERN: Generic error messages
except Exception as e:
    print("Database error")  # Lost all useful information
```

**Problems:**
- Cannot diagnose issues
- Silent data corruption
- Security issues go unnoticed
- Debugging becomes impossible

**Solution:** Log errors with full context

```python
# CORRECT: Proper error handling
try:
    cursor.execute(query, params)
except DatabaseError as e:
    logger.error(f"Database error: {e}", extra={
        'query': query,
        'params': params,
        'error_code': e.pgcode if hasattr(e, 'pgcode') else None
    })
    raise  # Re-raise or handle appropriately
```

---

#### 23. Diplomatic Immunity

**Objective:** Move fast in development

**Antipattern:** Treat database as second-class citizen

**Common manifestations:**
- No version control for schema changes
- No code review for SQL
- No testing for database logic
- Manual deployments
- Production access without audit trail

**Solution:** Apply software engineering best practices to database

```sql
-- Version-controlled migrations (e.g., using Flyway, Alembic, Liquibase)
-- V1__create_users_table.sql
CREATE TABLE users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(50) UNIQUE NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- V2__add_email_column.sql
ALTER TABLE users ADD COLUMN email VARCHAR(255);
```

**Best practices:**
- Version control all schema changes
- Code review database migrations
- Write tests for stored procedures and complex queries
- Use migrations for all schema changes (never manual DDL in production)
- Maintain separate environments (dev, staging, production)
- Implement audit logging for sensitive operations

---

#### 24. Magic Beans

**Objective:** Simplify application architecture

**Antipattern:** Treat Active Record models as the domain model

```python
# ANTI-PATTERN: Business logic in Active Record model
class User(ActiveRecord):
    def place_order(self, product, quantity):
        # Business logic mixed with data access
        if self.balance < product.price * quantity:
            raise InsufficientFundsError()

        order = Order.create(user_id=self.id, product_id=product.id)
        self.balance -= product.price * quantity
        self.save()
        return order
```

**Problems:**
- Business logic coupled to database structure
- Hard to test without database
- Domain model limited by ORM capabilities
- God objects that do too much

**Solution:** Separate domain models from data access

```python
# CORRECT: Service layer with separate concerns
class OrderService:
    def __init__(self, user_repository, order_repository, payment_service):
        self.user_repo = user_repository
        self.order_repo = order_repository
        self.payment = payment_service

    def place_order(self, user_id: int, product: Product, quantity: int) -> Order:
        user = self.user_repo.find(user_id)

        if not self.payment.can_afford(user, product.price * quantity):
            raise InsufficientFundsError()

        order = Order(user_id=user.id, product_id=product.id, quantity=quantity)
        self.order_repo.save(order)
        self.payment.charge(user, order.total)

        return order
```

---

### Quick Reference: Antipattern Categories

| Category | Antipattern | Key Symptom | Quick Fix |
|----------|-------------|-------------|-----------|
| **Logical Design** | Jaywalking | Comma-separated values in column | Intersection table |
| | Naive Trees | Only parent_id for hierarchies | Closure table |
| | ID Required | id column on every table | Use compound/natural keys where appropriate |
| | Keyless Entry | Missing foreign keys | Add FK constraints + indexes |
| | EAV | attribute_name/attribute_value columns | Proper typed columns or JSONB |
| | Polymorphic Associations | parent_type + parent_id columns | Common super-table |
| | Multicolumn Attributes | attribute1, attribute2, attribute3... | Dependent table |
| | Metadata Tribbles | table_2023, table_2024... | Native partitioning |
| **Physical Design** | Rounding Errors | FLOAT for money | DECIMAL/NUMERIC |
| | 31 Flavors | Hardcoded ENUMs | Lookup table |
| | Phantom Files | Only storing file paths | BLOB or managed external storage |
| | Index Shotgun | Random indexes | Analyze queries first |
| **Queries** | Fear of the Unknown | Incorrect NULL handling | IS NULL, COALESCE |
| | Ambiguous Groups | Non-aggregate in GROUP BY | Window functions |
| | Random Selection | ORDER BY RANDOM() | TABLESAMPLE or random key |
| | Poor Man's Search | LIKE '%keyword%' | Full-text search |
| | Spaghetti Query | Complex single query | Multiple simpler queries |
| | Implicit Columns | SELECT * | Explicit column names |
| **Application** | Readable Passwords | Plain text passwords | bcrypt/argon2 hashing |
| | SQL Injection | String concatenation | Parameterized queries |
| | Pseudokey Neat-Freak | Reusing deleted IDs | Accept gaps |
| | See No Evil | Suppressing errors | Log with full context |
| | Diplomatic Immunity | No DB best practices | Version control, migrations |
| | Magic Beans | ORM as domain model | Separate service layer |

---

## 18. Database Selection Guide: When to Use What

Choosing the right database is one of the most critical architectural decisions. There is no one-size-fits-all solution—the choice depends on data model, consistency requirements, scale, and team expertise.

### SQL vs NoSQL vs NewSQL Decision Framework

| Factor | SQL (Relational) | NoSQL | NewSQL |
|--------|------------------|-------|--------|
| **Data Structure** | Structured, well-defined relationships | Flexible, semi-structured, unstructured | Structured with flexibility |
| **Schema** | Fixed schema, strict | Schema-less or flexible | SQL-compatible, flexible |
| **Transactions** | Full ACID | Eventually consistent (mostly) | Full ACID |
| **Scaling** | Vertical (primarily) | Horizontal (built-in) | Horizontal with ACID |
| **Query Language** | SQL | Varies (MongoDB Query, CQL, etc.) | SQL |
| **Best For** | Complex queries, transactions | High throughput, flexible data | Global scale with consistency |

### When to Choose SQL (Relational)

```
✓ Data has clear relationships and structure
✓ ACID transactions are required
✓ Complex queries with JOINs are common
✓ Data integrity is critical (financial, healthcare)
✓ Team has SQL expertise
```

**Popular Options:**
- **PostgreSQL** - Most feature-rich, excellent for complex queries, JSONB support
- **MySQL** - Widely adopted, good read performance, proven scale (Facebook, Twitter)
- **SQL Server** - Enterprise features, .NET ecosystem integration
- **Oracle** - Enterprise-grade, excellent for high-value transactions

**Use Cases:** E-commerce, ERP, CRM, financial systems, inventory management

### When to Choose NoSQL

#### Document Databases (MongoDB, Couchbase)

```
✓ Data is hierarchical or document-oriented
✓ Schema evolves frequently
✓ Embedded documents reduce joins
✓ Horizontal scaling is required
```

**Use Cases:** Content management, user profiles, product catalogs, real-time analytics

```javascript
// MongoDB document example - natural fit for nested data
{
  "_id": "order_123",
  "customer": {
    "name": "John Doe",
    "email": "john@example.com"
  },
  "items": [
    { "product": "Widget", "qty": 2, "price": 29.99 },
    { "product": "Gadget", "qty": 1, "price": 49.99 }
  ],
  "total": 109.97,
  "status": "shipped"
}
```

#### Key-Value Stores (Redis, DynamoDB, Valkey)

```
✓ Simple lookup by key is primary access pattern
✓ Extremely high throughput required
✓ Caching layer needed
✓ Session management
```

**Use Cases:** Caching, session storage, real-time leaderboards, rate limiting

#### Wide-Column Stores (Cassandra, ScyllaDB, HBase)

```
✓ Time-series or event data
✓ Write-heavy workloads
✓ Multi-datacenter replication needed
✓ Linear scalability required
```

**Use Cases:** IoT data, messaging systems, activity feeds, audit logs

#### Graph Databases (Neo4j, Amazon Neptune)

```
✓ Data has complex, many-to-many relationships
✓ Relationship traversal is primary query pattern
✓ Social networks, recommendation engines
✓ Fraud detection, knowledge graphs
```

**Use Cases:** Social networks, recommendation engines, fraud detection, knowledge graphs

```cypher
// Neo4j Cypher query - find friends of friends who like similar products
MATCH (user:User {id: $userId})-[:FRIENDS_WITH*1..2]-(friend)
      -[:PURCHASED]->(product)<-[:PURCHASED]-(user)
WHERE user <> friend
RETURN DISTINCT friend, count(product) as commonPurchases
ORDER BY commonPurchases DESC
LIMIT 10
```

### When to Choose NewSQL

```
✓ Need ACID compliance AND horizontal scaling
✓ Global distribution with strong consistency
✓ SQL interface is required
✓ Cloud-native deployment preferred
```

**Popular Options:**
- **Google Spanner** - Global consistency, 99.999% availability
- **CockroachDB** - Spanner-inspired, PostgreSQL-compatible
- **TiDB** - MySQL-compatible, HTAP (hybrid transactional/analytical)
- **YugabyteDB** - PostgreSQL-compatible, multi-cloud

**Use Cases:** Global SaaS platforms, fintech, multi-region deployments

### Database Selection Decision Tree

```
START
  │
  ├── Is data highly relational with complex JOINs?
  │     ├── YES → Need global scale with ACID?
  │     │           ├── YES → NewSQL (Spanner, CockroachDB)
  │     │           └── NO → SQL (PostgreSQL, MySQL)
  │     │
  │     └── NO → What's the primary access pattern?
  │               │
  │               ├── Key-Value lookups → Redis, DynamoDB
  │               ├── Document/JSON → MongoDB, Couchbase
  │               ├── Time-series/Events → Cassandra, TimescaleDB
  │               ├── Graph traversal → Neo4j, Neptune
  │               └── Full-text search → Elasticsearch
  │
  └── Is ACID required?
        ├── YES → SQL or NewSQL
        └── NO → NoSQL for flexibility/scale
```

---

## 19. Modern Databases (2025): Specialized Solutions

### Time-Series Databases

For metrics, IoT, monitoring, and event data with time-based queries.

| Database | Best For | Performance Notes |
|----------|----------|-------------------|
| **ClickHouse** | Large-scale analytics, aggregations | 4.8x faster data loading, 3.8M+ QPS |
| **TimescaleDB** | SQL-familiar teams, PostgreSQL ecosystem | Best for batches < 1,000 rows, 90x faster DISTINCT |
| **InfluxDB** | Monitoring, DevOps metrics | Purpose-built for time-series, InfluxQL |
| **QuestDB** | Ultra-low latency analytics | SQL interface, extremely fast ingestion |

**Decision Guide:**
- **PostgreSQL user, moderate scale** → TimescaleDB
- **Massive aggregations, billions of rows** → ClickHouse
- **DevOps/monitoring focus** → InfluxDB
- **Need SQL + speed** → QuestDB

```sql
-- TimescaleDB: Automatic time partitioning
CREATE TABLE metrics (
    time        TIMESTAMPTZ NOT NULL,
    sensor_id   INTEGER,
    temperature DOUBLE PRECISION,
    humidity    DOUBLE PRECISION
);

SELECT create_hypertable('metrics', 'time');

-- ClickHouse: Columnar aggregations
SELECT
    toStartOfHour(timestamp) as hour,
    avg(value) as avg_value,
    count() as count
FROM metrics
WHERE timestamp > now() - INTERVAL 7 DAY
GROUP BY hour
ORDER BY hour;
```

### OLAP & Analytical Databases

| Database | Architecture | Best For |
|----------|--------------|----------|
| **ClickHouse** | Columnar, distributed | Real-time analytics, log analysis |
| **DuckDB** | Embedded, in-process | Local analytics, data science, notebooks |
| **Apache Druid** | Real-time OLAP | Sub-second queries on streaming data |
| **Snowflake** | Cloud-native, separated compute/storage | Data warehousing, multi-cloud |
| **BigQuery** | Serverless, columnar | Ad-hoc analytics, ML integration |

**DuckDB - The "SQLite of Analytics":**
```python
import duckdb

# Query Parquet files directly - no loading needed
result = duckdb.query("""
    SELECT
        category,
        SUM(amount) as total_sales
    FROM 'sales_*.parquet'
    GROUP BY category
    ORDER BY total_sales DESC
""").df()

# Query Pandas DataFrame in-place
import pandas as pd
df = pd.read_csv('large_file.csv')
duckdb.query("SELECT * FROM df WHERE amount > 1000").df()
```

### Edge & Embedded Databases

| Database | Use Case | Key Feature |
|----------|----------|-------------|
| **SQLite** | Mobile, embedded, local-first | Zero-config, serverless |
| **Turso** | Edge SQLite, global distribution | libSQL fork, edge replication |
| **LiteFS** | SQLite replication | Distributed SQLite for read replicas |
| **PocketBase** | Backend-in-a-box | SQLite + auth + real-time |

---

## 20. Big Tech Database Architectures

Understanding how tech giants solve database challenges at scale.

### Netflix

| Component | Database | Purpose |
|-----------|----------|---------|
| Metadata | Cassandra | Distributed, multi-region |
| Personalization | Cassandra | High-write throughput |
| Billing | MySQL | ACID transactions |
| Analytics | Redshift, Spark | Data warehousing |
| Caching | EVCache (Memcached) | Session, API responses |
| Search | Elasticsearch | Content discovery |

**Key Patterns:**
- Polyglot persistence - right database for each use case
- Regional replication with Cassandra
- Aggressive caching with EVCache
- Separation of operational and analytical workloads

### Meta (Facebook)

| Component | Database | Purpose |
|-----------|----------|---------|
| Social Graph | TAO (MySQL-based) | Graph operations at scale |
| Messages | HBase, MyRocks | High-write, time-series |
| Warehouse | Presto + HDFS | Analytical queries |
| Cache | Memcached (billions of keys) | Read-heavy workloads |
| AI/ML Features | RocksDB | Low-latency feature serving |

**Key Patterns:**
- Custom solutions built on MySQL (TAO, MyRocks)
- Massive caching layer (Memcached at unprecedented scale)
- Separate systems for social graph vs. messaging vs. analytics

### Google

| Component | Database | Purpose |
|-----------|----------|---------|
| Web Index | Bigtable | Massive key-value store |
| Ads, Finance | Spanner | Global ACID transactions |
| Analytics | BigQuery | Serverless data warehouse |
| Metadata | Megastore | Structured data with geo-replication |

**Key Patterns:**
- Invented foundational technologies (Bigtable, Spanner, BigQuery)
- Strong consistency at global scale (Spanner)
- Separation of storage and compute

### Amazon

| Component | Database | Purpose |
|-----------|----------|---------|
| Product Catalog | DynamoDB | Key-value, infinite scale |
| Orders | Aurora (MySQL/PostgreSQL) | Relational with cloud scale |
| Recommendations | Neptune | Graph-based recommendations |
| Analytics | Redshift | Data warehousing |
| Search | OpenSearch | Product search, logs |

**Key Patterns:**
- DynamoDB for everything that fits key-value model
- Aurora for relational needs with cloud elasticity
- Purpose-built databases for each workload

### Common Big Tech Patterns

1. **Polyglot Persistence**: Different databases for different access patterns
2. **Massive Caching**: Redis/Memcached in front of everything
3. **Read Replicas**: Scale reads horizontally
4. **Sharding**: Partition data for write scaling
5. **CQRS**: Separate read and write models
6. **Event Sourcing**: Audit trail and temporal queries
7. **Custom Solutions**: Build when off-the-shelf doesn't scale

---

## 21. Vector Databases for AI/ML Applications

Vector databases store and query high-dimensional embeddings for AI applications like semantic search, recommendations, and RAG (Retrieval Augmented Generation).

### When to Use Vector Databases

```
✓ Semantic/similarity search (not keyword-based)
✓ AI embeddings from models (OpenAI, Cohere, etc.)
✓ Recommendation systems
✓ Image/audio similarity search
✓ RAG applications with LLMs
✓ Anomaly detection
```

### Vector Database Comparison

| Database | Best For | Scale Limit | Self-Hosted |
|----------|----------|-------------|-------------|
| **Pinecone** | Managed simplicity, serverless | Billions | No |
| **Weaviate** | Hybrid search, GraphQL | Billions | Yes |
| **Milvus** | Massive scale, cost efficiency | Billions+ | Yes |
| **Qdrant** | Performance, Rust-based | Billions | Yes |
| **pgvector** | PostgreSQL integration | ~100M vectors | Yes |
| **Chroma** | Local development, prototyping | Millions | Yes |

### Decision Framework

```
Scale & Team Considerations:

Small scale (<10M vectors):
  └── Already using PostgreSQL? → pgvector
  └── Prototyping/local dev? → Chroma
  └── Want managed + easy? → Pinecone

Medium scale (10M-100M vectors):
  └── Need hybrid search? → Weaviate
  └── Want performance + control? → Qdrant
  └── Budget-conscious? → Self-hosted Qdrant/Weaviate

Large scale (>100M vectors):
  └── Have data engineering team? → Milvus
  └── Need managed + SLAs? → Pinecone (serverless)
  └── Cost-sensitive at scale? → Milvus/Qdrant self-hosted
```

### pgvector - When PostgreSQL Is Enough

```sql
-- Enable the extension
CREATE EXTENSION vector;

-- Create table with vector column
CREATE TABLE documents (
    id SERIAL PRIMARY KEY,
    content TEXT,
    embedding vector(1536)  -- OpenAI ada-002 dimension
);

-- Create index for fast similarity search
CREATE INDEX ON documents USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- Similarity search
SELECT id, content,
       1 - (embedding <=> query_embedding) as similarity
FROM documents
ORDER BY embedding <=> '[0.1, 0.2, ...]'::vector
LIMIT 10;
```

**pgvector Limitations:**
- Realistic max: 10-100M vectors before performance degrades
- Index build time increases significantly at scale
- No built-in sharding

### Dedicated Vector Database Example (Pinecone)

```python
import pinecone
from openai import OpenAI

# Initialize
pinecone.init(api_key="YOUR_KEY", environment="us-west1-gcp")
index = pinecone.Index("documents")
openai = OpenAI()

# Upsert vectors
def upsert_document(doc_id: str, text: str, metadata: dict):
    embedding = openai.embeddings.create(
        model="text-embedding-ada-002",
        input=text
    ).data[0].embedding

    index.upsert([(doc_id, embedding, metadata)])

# Query
def semantic_search(query: str, top_k: int = 10):
    query_embedding = openai.embeddings.create(
        model="text-embedding-ada-002",
        input=query
    ).data[0].embedding

    results = index.query(
        vector=query_embedding,
        top_k=top_k,
        include_metadata=True
    )
    return results.matches
```

### Hybrid Search (Vector + Keyword)

Combine semantic understanding with keyword precision:

```python
# Weaviate hybrid search
result = client.query.get("Document", ["content", "title"]) \
    .with_hybrid(
        query="machine learning best practices",
        alpha=0.5  # 0 = pure keyword, 1 = pure vector
    ) \
    .with_limit(10) \
    .do()
```

---

## 22. Database Scaling Strategies: Startup to Enterprise

### Scaling Progression (Don't Over-Engineer Early)

```
Stage 1: Single Database (0 - 10K users)
    │
    │  Problems: None yet
    │  Solution: Don't optimize prematurely
    │
Stage 2: Vertical Scaling (10K - 100K users)
    │
    │  Problems: Slow queries, high CPU
    │  Solutions:
    │    - Add indexes (biggest impact)
    │    - Upgrade instance size
    │    - Query optimization
    │
Stage 3: Read Replicas + Caching (100K - 1M users)
    │
    │  Problems: Read bottlenecks
    │  Solutions:
    │    - Add read replicas
    │    - Implement Redis caching
    │    - Connection pooling (PgBouncer)
    │
Stage 4: Functional Partitioning (1M - 10M users)
    │
    │  Problems: Mixed workloads compete
    │  Solutions:
    │    - Separate OLTP from OLAP
    │    - Move analytics to dedicated DB
    │    - Archive old data
    │
Stage 5: Sharding (10M+ users)
    │
    │  Problems: Single-master write bottleneck
    │  Solutions:
    │    - Horizontal sharding
    │    - Multi-master (CockroachDB, Vitess)
    │    - Consider NoSQL for some workloads
```

### Stage-by-Stage Implementation

#### Stage 2: Indexing (Biggest Bang for Buck)

```sql
-- Find slow queries (PostgreSQL)
SELECT query, calls, mean_time, total_time
FROM pg_stat_statements
ORDER BY mean_time DESC
LIMIT 20;

-- Add strategic indexes
CREATE INDEX CONCURRENTLY idx_orders_customer_date
    ON orders(customer_id, created_at DESC);

-- Partial indexes for common filters
CREATE INDEX idx_orders_pending
    ON orders(created_at)
    WHERE status = 'pending';
```

#### Stage 3: Read Replicas

```python
# Application-level read/write splitting
class DatabaseRouter:
    def __init__(self, writer_url: str, reader_urls: list[str]):
        self.writer = create_engine(writer_url)
        self.readers = [create_engine(url) for url in reader_urls]
        self._reader_index = 0

    def get_writer(self):
        return self.writer

    def get_reader(self):
        # Round-robin reader selection
        reader = self.readers[self._reader_index]
        self._reader_index = (self._reader_index + 1) % len(self.readers)
        return reader

# Usage
router = DatabaseRouter(
    writer_url="postgresql://primary:5432/db",
    reader_urls=[
        "postgresql://replica1:5432/db",
        "postgresql://replica2:5432/db"
    ]
)

# Writes go to primary
with router.get_writer().connect() as conn:
    conn.execute("INSERT INTO orders ...")

# Reads go to replicas
with router.get_reader().connect() as conn:
    result = conn.execute("SELECT * FROM orders WHERE ...")
```

#### Stage 3: Caching Layer

```python
import redis
import json
from functools import wraps

redis_client = redis.Redis(host='localhost', port=6379, db=0)

def cache(ttl_seconds: int = 300):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            cache_key = f"{func.__name__}:{hash((args, tuple(kwargs.items())))}"

            # Try cache first
            cached = redis_client.get(cache_key)
            if cached:
                return json.loads(cached)

            # Execute and cache
            result = func(*args, **kwargs)
            redis_client.setex(cache_key, ttl_seconds, json.dumps(result))
            return result
        return wrapper
    return decorator

@cache(ttl_seconds=60)
def get_user_profile(user_id: int) -> dict:
    # This database query is cached for 60 seconds
    return db.query("SELECT * FROM users WHERE id = %s", user_id)
```

#### Stage 5: Sharding Strategies

**Hash-Based Sharding:**
```python
def get_shard(user_id: int, num_shards: int = 16) -> int:
    return user_id % num_shards

def get_connection(user_id: int):
    shard_id = get_shard(user_id)
    return connection_pools[f"shard_{shard_id}"]
```

**Range-Based Sharding:**
```python
def get_shard_by_date(created_at: datetime) -> str:
    if created_at.year < 2023:
        return "shard_archive"
    elif created_at.year == 2023:
        return "shard_2023"
    else:
        return "shard_current"
```

### Caching Database Comparison

| Database | Best For | Throughput | Persistence |
|----------|----------|------------|-------------|
| **Redis** | General caching, data structures | ~100K QPS/node | Yes (RDB/AOF) |
| **Memcached** | Simple key-value caching | Higher than Redis | No |
| **Dragonfly** | Redis replacement, multi-threaded | 25x Redis (3.8M QPS) | Yes |
| **KeyDB** | Multi-threaded Redis fork | 5x Redis | Yes |

**Dragonfly vs Redis:**
- Dragonfly: 25x throughput, 80% fewer resources, drop-in Redis replacement
- Redis: Mature ecosystem, extensive tooling, recent licensing returned to open source (AGPLv3)
- For new deployments with high throughput needs, evaluate Dragonfly

### Connection Pooling (Critical at Scale)

```python
# PgBouncer configuration for PostgreSQL
# /etc/pgbouncer/pgbouncer.ini

[databases]
mydb = host=localhost port=5432 dbname=mydb

[pgbouncer]
listen_addr = *
listen_port = 6432
auth_type = md5
auth_file = /etc/pgbouncer/userlist.txt

# Pool settings
pool_mode = transaction  # or 'session' for long transactions
max_client_conn = 1000
default_pool_size = 20
min_pool_size = 5
reserve_pool_size = 5
```

### When to Consider Database Migration

| Current State | Problem | Consider Migration To |
|---------------|---------|----------------------|
| Single PostgreSQL, >1TB | Write bottleneck | CockroachDB, Vitess (MySQL), Citus |
| MySQL with complex analytics | Analytics killing OLTP | Add ClickHouse/BigQuery for analytics |
| MongoDB at scale | Need transactions | PostgreSQL or add SQL for transactional data |
| Single-region, global users | Latency | Spanner, CockroachDB (multi-region) |

---

## 23. Anti-Patterns in Database Selection

### Anti-Pattern 1: Using Relational DB for Everything

```
❌ Problem: Forcing graph data into SQL
   - Friend-of-friend queries require recursive CTEs
   - Performance degrades exponentially with depth

✓ Solution: Use graph database for relationship-heavy data
```

### Anti-Pattern 2: NoSQL for ACID-Required Workloads

```
❌ Problem: Financial transactions in MongoDB
   - Risk of data inconsistency
   - Complex application-level compensation logic

✓ Solution: Use PostgreSQL/MySQL or NewSQL (CockroachDB)
```

### Anti-Pattern 3: Premature Sharding

```
❌ Problem: Sharding a database with 10GB of data
   - Added complexity with no benefit
   - Cross-shard queries become expensive

✓ Solution: Scale vertically first, shard only when necessary
   Rule of thumb: Consider sharding at >500GB or >50K writes/sec
```

### Anti-Pattern 4: Ignoring the Caching Layer

```
❌ Problem: Every request hits the database
   - User profile fetched on every page load
   - Database becomes bottleneck

✓ Solution: Cache aggressively
   - Cache user sessions: Redis with 24h TTL
   - Cache frequently-read data: 5-60 minute TTL
   - Use read-through/write-through patterns
```

### Anti-Pattern 5: One Database for OLTP + OLAP

```
❌ Problem: Running analytics on production database
   - Long-running queries block transactions
   - Index requirements conflict

✓ Solution: Separate operational and analytical workloads
   - OLTP: PostgreSQL/MySQL
   - OLAP: ClickHouse/BigQuery/Redshift
   - Connect via CDC (Change Data Capture) or ETL
```

### Anti-Pattern 6: Vector Search on Traditional DB at Scale

```
❌ Problem: Using pgvector with 500M vectors
   - Index build takes hours
   - Query latency exceeds SLAs

✓ Solution: Purpose-built vector database
   - <100M vectors: pgvector is fine
   - >100M vectors: Milvus, Pinecone, Qdrant
```

---

## 24. Serverless PostgreSQL Platforms: Neon, Supabase, PlanetScale

The serverless database landscape has matured significantly, with specialized platforms offering different value propositions.

### Platform Comparison

| Feature | Neon | Supabase | PlanetScale (Postgres) |
|---------|------|----------|------------------------|
| **Core** | Serverless PostgreSQL | BaaS with PostgreSQL | MySQL + new Postgres offering |
| **Scale to Zero** | ✅ Yes | ❌ No | ❌ No |
| **Database Branching** | ✅ Instant (copy-on-write) | ✅ Git-integrated | ✅ Yes |
| **Built-in Auth** | ❌ No | ✅ Yes | ❌ No |
| **Real-time** | ❌ No | ✅ Yes | ❌ No |
| **Edge Functions** | ❌ No | ✅ Yes | ❌ No |
| **Vector Support** | ✅ pgvector | ✅ pgvector | ✅ (MySQL native) |
| **Compliance** | SOC2 Type 2 | SOC2 + HIPAA | SOC2 |

### Neon: True Serverless PostgreSQL

**Architecture:** Neon separates compute and storage. Compute is standard Postgres; storage is a custom multi-tenant system with copy-on-write branching.

```
Key Benefits:
- Scale to zero (pay nothing when idle)
- Instant database branching for dev/test
- Sub-second cold starts
- Autoscaling compute
```

**Recent News (2025):** Databricks acquired Neon for ~$1 billion. Over 80% of Neon databases are now created by AI agents automatically.

```javascript
// Neon with serverless driver (edge-compatible)
import { neon } from '@neondatabase/serverless';

const sql = neon(process.env.DATABASE_URL);

// Works in Vercel Edge, Cloudflare Workers, etc.
const users = await sql`SELECT * FROM users WHERE id = ${userId}`;
```

### Supabase: Firebase Alternative on PostgreSQL

**Architecture:** Battery-included platform with vanilla Postgres core + middleware (PostgREST, GoTrue, Realtime, Storage).

```
Key Benefits:
- Complete backend (auth, storage, functions)
- Real-time subscriptions out of the box
- Auto-generated REST & GraphQL APIs
- 400+ extensions pre-configured
```

**Modern Extensions Included:**
- `pg_graphql`: GraphQL queries via SQL function
- `pg_jsonschema`: JSON Schema validation
- `pgvector`: Vector similarity search
- `pg_net`: HTTP requests from SQL
- `vault`: Secrets management

```sql
-- Supabase: Auto-generated API + Real-time
-- This table automatically gets REST API + real-time subscriptions

CREATE TABLE messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    content TEXT NOT NULL,
    user_id UUID REFERENCES auth.users(id),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Enable real-time for this table
ALTER PUBLICATION supabase_realtime ADD TABLE messages;

-- Enable Row Level Security
ALTER TABLE messages ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can read all messages"
    ON messages FOR SELECT USING (true);

CREATE POLICY "Users can insert own messages"
    ON messages FOR INSERT WITH CHECK (auth.uid() = user_id);
```

```javascript
// Supabase client with real-time
import { createClient } from '@supabase/supabase-js';

const supabase = createClient(SUPABASE_URL, SUPABASE_ANON_KEY);

// Real-time subscription
supabase
  .channel('messages')
  .on('postgres_changes', { event: 'INSERT', schema: 'public', table: 'messages' },
    (payload) => console.log('New message:', payload.new))
  .subscribe();
```

### PlanetScale PostgreSQL (2025)

**Recent Development:** PlanetScale launched managed PostgreSQL in late 2025, built on "Metal" clusters with local NVMe drives.

```
Key Benefits:
- "Unlimited I/O" pricing model
- Low-latency Metal clusters
- Database branching
- Established MySQL expertise applied to Postgres
```

### Decision Framework

```
Choose Neon when:
├── Serverless/edge deployment required
├── Development workflows need instant branching
├── Cost optimization via scale-to-zero is important
└── AI agents creating databases programmatically

Choose Supabase when:
├── Need complete backend (auth, storage, functions)
├── Real-time features are required
├── Want to avoid building APIs manually
└── Prefer managed, batteries-included approach

Choose PlanetScale when:
├── Already using PlanetScale for MySQL
├── Need "Unlimited I/O" pricing model
└── Prioritize raw I/O performance
```

---

## 25. Modern PostgreSQL Extensions (2025)

### Vector & AI Extensions

#### pgvectorscale (Timescale)

**Purpose:** High-performance vector search extension that complements pgvector with DiskANN-based indexing.

**Performance:** 28x lower p95 latency and 16x higher throughput vs Pinecone at 75% less cost on 50M vectors.

```sql
-- Install extensions
CREATE EXTENSION IF NOT EXISTS vector;        -- Base pgvector
CREATE EXTENSION IF NOT EXISTS vectorscale;   -- Enhanced indexing

-- Create table with vector column
CREATE TABLE documents (
    id SERIAL PRIMARY KEY,
    content TEXT,
    embedding vector(1536)
);

-- Create StreamingDiskANN index (pgvectorscale)
CREATE INDEX ON documents
USING diskann (embedding vector_cosine_ops);

-- Query with high performance
SELECT id, content,
       embedding <=> '[0.1, 0.2, ...]'::vector AS distance
FROM documents
ORDER BY embedding <=> '[0.1, 0.2, ...]'::vector
LIMIT 10;
```

**Key Technologies:**
- **StreamingDiskANN:** Microsoft DiskANN-inspired index for billion-scale vectors
- **Statistical Binary Quantization (SBQ):** Better than standard binary quantization
- **Filtered search:** Label-based filtering during vector search

**Limitations:** Index build is currently single-threaded (~11 hours for 50M vectors vs 3.3 hours for Qdrant).

#### pgai (Timescale)

**Purpose:** AI workflows directly in PostgreSQL - embeddings, RAG, model inference.

```sql
-- Generate embeddings directly in SQL
SELECT ai.create_embedding(
    'openai/text-embedding-3-small',
    'Your text to embed'
);

-- Semantic search with auto-embedding
SELECT * FROM ai.semantic_search(
    'documents',
    'embedding',
    'Find documents about machine learning',
    limit_val => 10
);
```

### Search & Analytics Extensions

#### pg_search (ParadeDB)

**Purpose:** Elasticsearch-quality full-text search inside PostgreSQL using BM25 algorithm and Tantivy (Rust-based Lucene alternative).

```sql
CREATE EXTENSION pg_search;

-- Create BM25 index
CREATE INDEX idx_products_search ON products
USING bm25 (name, description)
WITH (key_field = 'id');

-- Full-text search with BM25 scoring
SELECT id, name, description, paradedb.score(id) as relevance
FROM products
WHERE name @@@ 'wireless headphones'
   OR description @@@ 'bluetooth audio'
ORDER BY paradedb.score(id) DESC
LIMIT 20;

-- Hybrid search (BM25 + vector)
SELECT *,
    (0.5 * paradedb.score(id) + 0.5 * (1 - (embedding <=> query_vec))) as hybrid_score
FROM products
WHERE name @@@ 'headphones'
ORDER BY hybrid_score DESC;
```

**Adoption:** 400,000+ deployments, used by Alibaba Cloud and Bilt Rewards ($36B+ payments processed).

#### pg_duckdb (Hydra + MotherDuck)

**Purpose:** Embed DuckDB's columnar analytics engine inside PostgreSQL for 10-1500x faster analytical queries.

```sql
CREATE EXTENSION pg_duckdb;

-- Query Parquet files directly from S3
SELECT
    category,
    SUM(amount) as total_sales,
    COUNT(*) as transactions
FROM read_parquet('s3://bucket/sales/*.parquet')
GROUP BY category
ORDER BY total_sales DESC;

-- Use DuckDB syntax directly
SELECT * FROM duckdb.query($$
    SELECT *
    FROM read_csv_auto('s3://bucket/data.csv')
    WHERE amount > 1000
$$);

-- Create analytics-optimized table
CREATE TABLE analytics_data (
    id SERIAL,
    event_time TIMESTAMPTZ,
    user_id INTEGER,
    event_type TEXT,
    properties JSONB
) USING duckdb;
```

**Performance:** Up to 1500x improvement for analytical queries; realistic 10x for many workloads.

### Automation & Operations Extensions

#### pg_cron

**Purpose:** Schedule jobs directly in PostgreSQL using cron syntax.

```sql
CREATE EXTENSION pg_cron;

-- Schedule daily cleanup at 3 AM
SELECT cron.schedule(
    'cleanup-old-sessions',
    '0 3 * * *',
    $$DELETE FROM sessions WHERE expires_at < NOW() - INTERVAL '7 days'$$
);

-- Schedule hourly aggregation
SELECT cron.schedule(
    'hourly-metrics',
    '0 * * * *',
    $$INSERT INTO metrics_hourly
      SELECT date_trunc('hour', created_at), count(*)
      FROM events
      WHERE created_at > NOW() - INTERVAL '2 hours'
      GROUP BY 1
      ON CONFLICT (hour) DO UPDATE SET count = EXCLUDED.count$$
);

-- Run in different database
SELECT cron.schedule_in_database(
    'analytics-job',
    '*/15 * * * *',
    $$REFRESH MATERIALIZED VIEW CONCURRENTLY sales_summary$$,
    'analytics_db'
);

-- List scheduled jobs
SELECT * FROM cron.job;

-- Unschedule
SELECT cron.unschedule('cleanup-old-sessions');
```

#### pg_partman

**Purpose:** Automated partition management for time-series and large tables.

```sql
CREATE EXTENSION pg_partman;

-- Create parent partitioned table
CREATE TABLE events (
    id BIGSERIAL,
    event_time TIMESTAMPTZ NOT NULL,
    event_type TEXT,
    payload JSONB
) PARTITION BY RANGE (event_time);

-- Configure automatic partition management
SELECT partman.create_parent(
    p_parent_table := 'public.events',
    p_control := 'event_time',
    p_interval := 'daily',
    p_premake := 7  -- Create 7 days of future partitions
);

-- Auto-maintenance via pg_cron
SELECT cron.schedule(
    'partition-maintenance',
    '0 1 * * *',
    $$SELECT partman.run_maintenance()$$
);
```

### API & Integration Extensions

#### pg_graphql (Supabase)

**Purpose:** GraphQL API directly from PostgreSQL schema.

```sql
CREATE EXTENSION pg_graphql;

-- GraphQL is auto-generated from your schema
-- Query via SQL function
SELECT graphql.resolve($$
    query {
        users(first: 10) {
            edges {
                node {
                    id
                    email
                    posts {
                        edges {
                            node {
                                title
                            }
                        }
                    }
                }
            }
        }
    }
$$);
```

#### pg_jsonschema (Supabase)

**Purpose:** JSON Schema validation for JSONB columns.

```sql
CREATE EXTENSION pg_jsonschema;

-- Add JSON Schema validation constraint
ALTER TABLE products
ADD CONSTRAINT valid_metadata CHECK (
    jsonb_matches_schema(
        '{
            "type": "object",
            "properties": {
                "weight": {"type": "number", "minimum": 0},
                "dimensions": {
                    "type": "object",
                    "properties": {
                        "length": {"type": "number"},
                        "width": {"type": "number"},
                        "height": {"type": "number"}
                    },
                    "required": ["length", "width", "height"]
                }
            },
            "required": ["weight"]
        }'::json,
        metadata
    )
);
```

### Extension Ecosystem Summary

| Category | Extension | Purpose | Performance Impact |
|----------|-----------|---------|-------------------|
| **Vector** | pgvector | Base vector operations | Baseline |
| **Vector** | pgvectorscale | DiskANN indexing | 28x faster than Pinecone |
| **Vector** | pgai | AI workflows in SQL | Simplifies AI pipelines |
| **Search** | pg_search | BM25 full-text search | Elasticsearch-quality |
| **Analytics** | pg_duckdb | Columnar analytics | 10-1500x faster |
| **Scheduling** | pg_cron | Job scheduling | N/A |
| **Partitioning** | pg_partman | Auto partition management | N/A |
| **API** | pg_graphql | GraphQL from schema | Zero API code |
| **Validation** | pg_jsonschema | JSON Schema validation | N/A |

---

## 26. Connection Pooling: PgBouncer vs PgCat vs Supavisor

Connection pooling is critical for scaling PostgreSQL. Modern alternatives offer significant improvements over traditional PgBouncer.

### Comparison Overview

| Feature | PgBouncer | PgCat | Supavisor |
|---------|-----------|-------|-----------|
| **Language** | C (libevent) | Rust (Tokio) | Elixir (BEAM) |
| **Threading** | Single-threaded | Multi-threaded | Multi-process (BEAM) |
| **Max Connections** | ~1000/instance | Millions | 1 Million+ tested |
| **Prepared Statements** | Limited | ✅ Full support | ✅ Full support |
| **Query Load Balancing** | ❌ No | ✅ Yes | ✅ Yes |
| **Replica Lag Awareness** | ❌ No | ✅ Yes | ✅ Yes |
| **Hot Reload** | Requires restart | ✅ Zero downtime | ✅ Zero downtime |
| **Multi-tenancy** | ❌ No | ✅ Yes | ✅ Yes (designed for) |

### PgBouncer (Traditional Choice)

**Best for:** Simple deployments, low-to-medium connection counts (<50 clients).

```ini
# /etc/pgbouncer/pgbouncer.ini
[databases]
mydb = host=localhost port=5432 dbname=mydb

[pgbouncer]
listen_addr = *
listen_port = 6432
auth_type = md5
auth_file = /etc/pgbouncer/userlist.txt

# Pool configuration
pool_mode = transaction
max_client_conn = 1000
default_pool_size = 20
min_pool_size = 5
reserve_pool_size = 5

# Performance tuning
max_db_connections = 100
```

**Limitations:**
- Single-threaded: CPU maxes at ~50 clients, need multiple instances
- No built-in load balancing to replicas
- Configuration changes require restart

### PgCat (Modern Rust Alternative)

**Best for:** High-throughput applications needing query routing and replica awareness.

```toml
# pgcat.toml
[general]
host = "0.0.0.0"
port = 6432
admin_username = "admin"
admin_password = "admin"

[pools.mydb]
pool_mode = "transaction"
default_role = "primary"
query_parser_enabled = true
primary_reads_enabled = false
sharding_function = "pg_bigint_hash"

[pools.mydb.users.app]
password = "secret"
pool_size = 20
min_pool_size = 5

[pools.mydb.shards.0]
database = "mydb"
servers = [
    ["primary.db.com", 5432, "primary"],
    ["replica1.db.com", 5432, "replica"],
    ["replica2.db.com", 5432, "replica"],
]
```

**Performance:**
- 59K TPS vs PgBouncer's 44K TPS at high concurrency
- Handles 1,250 connections within 400% CPU (4 cores)
- PgBouncer hits 100% CPU at 50 connections (1 core)

**Key Features:**
```toml
# Replica lag awareness
[pools.mydb]
replica_selection_strategy = "least_lag"
max_replica_lag_ms = 1000  # Don't route to replicas lagging >1s

# Query routing
[pools.mydb]
query_parser_enabled = true
primary_reads_enabled = false  # All reads to replicas

# Sharding support
[pools.mydb]
sharding_function = "pg_bigint_hash"
shards = 4
```

### Supavisor (Cloud-Native Scale)

**Best for:** Massive scale (1M+ connections), multi-tenant SaaS, serverless environments.

**Architecture:** Built in Elixir on the BEAM VM, designed for massive concurrency with lightweight processes.

```elixir
# Supavisor configuration (simplified)
config :supavisor,
  # Cluster configuration
  cluster_postgres: [
    primary: "postgresql://primary:5432/db",
    replicas: [
      "postgresql://replica1:5432/db",
      "postgresql://replica2:5432/db"
    ]
  ],
  # Pool settings
  pool_size: 20,
  pool_mode: :transaction,
  # Multi-tenant support
  tenant_pool_size: 10,
  max_tenants_per_node: 10000
```

**Unique Features:**
- **Dynamic tenant pools:** Pools created on-demand per tenant
- **1M+ connections tested:** Horizontal scaling across cluster
- **Read-after-write consistency:** Smart routing ensures consistency
- **Query cancellation:** Cancel long-running queries easily

### Decision Matrix

```
Connection Count & Use Case:

< 50 concurrent connections:
└── PgBouncer (simple, proven, minimal resources)

50-1000 connections + need query routing:
└── PgCat (modern features, better throughput)

1000+ connections OR multi-tenant SaaS:
└── Supavisor (designed for massive scale)

Serverless/Edge with many short-lived connections:
└── Supavisor or PgCat

Need replica load balancing + lag awareness:
└── PgCat or Supavisor (not PgBouncer)
```

### Connection Pooling Best Practices

```python
# Application-side: Use connection pooling library
from sqlalchemy import create_engine
from sqlalchemy.pool import QueuePool

# Connect through pooler, not directly to Postgres
engine = create_engine(
    "postgresql://user:pass@pgcat-host:6432/mydb",
    poolclass=QueuePool,
    pool_size=5,           # App-side pool (small, pooler handles rest)
    max_overflow=10,
    pool_pre_ping=True,    # Verify connections before use
    pool_recycle=3600,     # Recycle connections hourly
)

# For serverless: Use serverless-compatible drivers
# Neon serverless driver (works in edge)
from neon import neon

sql = neon(DATABASE_URL)
result = await sql("SELECT * FROM users WHERE id = $1", [user_id])
```

---

## 27. Local-First & Edge Databases

The local-first paradigm is transforming how applications handle data, enabling offline-capable apps with real-time sync.

### ElectricSQL: PostgreSQL Sync Engine

**Purpose:** Real-time sync between PostgreSQL and client-side databases (SQLite, IndexedDB).

**Architecture:**
```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│   PostgreSQL    │────▶│  Electric Sync   │────▶│  Client App     │
│   (Source of    │     │    Service       │     │  (PGlite/SQLite)│
│    Truth)       │◀────│  (HTTP Stream)   │◀────│                 │
└─────────────────┘     └──────────────────┘     └─────────────────┘
         │                                                │
         │              Logical Replication               │
         └────────────────────────────────────────────────┘
```

```typescript
// ElectricSQL client setup
import { electrify } from 'electric-sql/wa-sqlite';
import { schema } from './generated/client';

// Connect to local SQLite + Electric sync
const electric = await electrify(db, schema, {
  url: 'https://api.electric-sql.com',
  auth: { token: authToken }
});

// Subscribe to a "shape" of data
const { synced } = await electric.db.projects.sync({
  where: { user_id: currentUser.id },
  include: { tasks: true }
});

// Wait for initial sync
await synced;

// Now query locally (instant, no network)
const projects = await electric.db.projects.findMany({
  include: { tasks: true }
});

// Writes go to local first, sync in background
await electric.db.tasks.create({
  data: { title: 'New task', project_id: projectId }
});
```

**Key Features:**
- Shapes: Subscribe to subsets of data
- Conflict resolution: Built on CRDTs
- Works offline: Full functionality without network
- 600,000+ weekly downloads

### PGlite: PostgreSQL in WebAssembly

**Purpose:** Run actual PostgreSQL in the browser, Node.js, or Deno. Only 3MB gzipped.

```typescript
// Browser usage with IndexedDB persistence
import { PGlite } from '@electric-sql/pglite';

// In-memory database
const db = new PGlite();

// Or with persistence
const db = new PGlite('idb://my-database');

// Full PostgreSQL SQL support
await db.exec(`
  CREATE TABLE IF NOT EXISTS todos (
    id SERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    completed BOOLEAN DEFAULT FALSE
  )
`);

// Parameterized queries
const result = await db.query(
  'INSERT INTO todos (title) VALUES ($1) RETURNING *',
  ['Buy groceries']
);

// Extensions support (including pgvector!)
import { vector } from '@electric-sql/pglite/vector';

const db = new PGlite({
  extensions: { vector }
});

await db.exec('CREATE EXTENSION IF NOT EXISTS vector');
await db.exec(`
  CREATE TABLE documents (
    id SERIAL PRIMARY KEY,
    embedding vector(384)
  )
`);
```

**Use Cases:**
- Unit/CI testing (instant startup/teardown)
- Offline-first applications
- Local development without Docker
- AI/ML pipelines in browser

### FerretDB: MongoDB API on PostgreSQL

**Purpose:** MongoDB wire protocol compatibility backed by PostgreSQL (via DocumentDB extension).

```javascript
// Use standard MongoDB driver
const { MongoClient } = require('mongodb');

// Connect to FerretDB (which connects to PostgreSQL)
const client = new MongoClient('mongodb://localhost:27017');
await client.connect();

const db = client.db('mydb');
const collection = db.collection('users');

// Standard MongoDB operations
await collection.insertOne({
  name: 'John',
  email: 'john@example.com',
  tags: ['developer', 'postgres-fan']
});

// Queries work as expected
const users = await collection.find({ tags: 'developer' }).toArray();

// Aggregation pipelines
const result = await collection.aggregate([
  { $match: { tags: 'developer' } },
  { $group: { _id: '$name', count: { $sum: 1 } } }
]).toArray();
```

**FerretDB 2.0 (2025) Improvements:**
- 20x faster with Microsoft's DocumentDB extension
- Vector search support (HNSW)
- Sub-millisecond P99 latency for point lookups
- Replication support

**When to Use:**
- Migrating from MongoDB to PostgreSQL
- Want MongoDB DX with PostgreSQL reliability
- Need open-source MongoDB alternative (Apache 2.0 license)

### Local-First Architecture Patterns

```typescript
// Pattern: Optimistic UI with background sync
class LocalFirstStore {
  private local: PGlite;
  private sync: ElectricSync;
  private pendingWrites: Map<string, WriteOperation>;

  async write(operation: WriteOperation) {
    // 1. Write to local immediately (optimistic)
    await this.local.exec(operation.sql);

    // 2. Update UI immediately
    this.notifyListeners(operation);

    // 3. Queue for sync
    this.pendingWrites.set(operation.id, operation);

    // 4. Sync in background (non-blocking)
    this.syncToServer(operation).catch(this.handleSyncError);
  }

  async read(query: string) {
    // Always read from local (instant)
    return this.local.query(query);
  }
}

// Pattern: Conflict resolution
const electric = await electrify(db, schema, {
  // Last-write-wins (default)
  conflictResolution: 'lww',

  // Or custom resolution
  conflictResolver: (local, remote) => {
    // Merge changes intelligently
    return { ...remote, ...local, updatedAt: new Date() };
  }
});
```

---

## 28. PostgreSQL Storage Engines & Future

### OrioleDB: Next-Generation Storage Engine

**Purpose:** Modern storage engine using PostgreSQL's Table Access Method API, eliminating bloat and improving performance.

**Key Innovations:**
- **No bloat:** UNDO log instead of storing old tuples in main table
- **No vacuum needed:** Page merging instead of garbage collection
- **64-bit transaction IDs:** No wraparound problem
- **Lock-less page reading:** Direct links between memory and storage pages

```sql
-- Install OrioleDB
CREATE EXTENSION orioledb;

-- Create table with OrioleDB storage
CREATE TABLE high_write_table (
    id BIGSERIAL PRIMARY KEY,
    data JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
) USING orioledb;

-- Or set as default for all new tables
SET default_table_access_method = 'orioledb';

CREATE TABLE another_table (
    id SERIAL PRIMARY KEY,
    name TEXT
);  -- Automatically uses OrioleDB
```

**Performance (2025 benchmarks):**
- 2x QPS on TPC-C benchmark (19,000 vs 9,500 QPS)
- 3.3x speedup on transactional workloads
- Significant reduction in storage bloat

**Current Status:**
- Beta (targeting GA in 2025)
- Requires patches to PostgreSQL core
- Supported by Supabase (OrioleDB-17 available)

### Comparison: Heap vs OrioleDB

| Aspect | Heap (Default) | OrioleDB |
|--------|----------------|----------|
| **MVCC** | Old tuples in table | UNDO log |
| **Bloat** | Requires VACUUM | No bloat (page merging) |
| **Transaction IDs** | 32-bit (wraparound) | 64-bit (no wraparound) |
| **Buffer Mapping** | Required | Lock-less direct links |
| **Write Amplification** | Higher | Lower |
| **Maturity** | Production-ready | Beta |

### The Future: Postgres as Universal Platform

```
PostgreSQL Ecosystem Evolution (2025+):

┌─────────────────────────────────────────────────────────────────┐
│                    PostgreSQL Core                               │
├─────────────────────────────────────────────────────────────────┤
│  Storage Engines    │  Execution Engines   │  Data Types        │
│  ├── Heap (default) │  ├── Native Postgres │  ├── Standard SQL  │
│  ├── OrioleDB       │  ├── DuckDB (OLAP)   │  ├── JSONB         │
│  └── Future...      │  └── Velox (future?) │  ├── Vector        │
│                     │                      │  └── BSON (FerretDB)│
├─────────────────────────────────────────────────────────────────┤
│  Sync & Replication │  APIs & Protocols    │  Scheduling        │
│  ├── Logical Rep    │  ├── SQL             │  ├── pg_cron       │
│  ├── ElectricSQL    │  ├── REST (PostgREST)│  └── pg_partman    │
│  └── BDR/Citus      │  ├── GraphQL         │                    │
│                     │  └── MongoDB Wire    │                    │
├─────────────────────────────────────────────────────────────────┤
│  Connection Pooling │  Search & AI         │  Cloud Platforms   │
│  ├── PgBouncer      │  ├── pg_search (BM25)│  ├── Supabase      │
│  ├── PgCat          │  ├── pgvector        │  ├── Neon          │
│  └── Supavisor      │  └── pgvectorscale   │  └── Others        │
└─────────────────────────────────────────────────────────────────┘
```

### Modern PostgreSQL Stack Recommendations

**For Startups (0-1M users):**
```
Database:    Supabase or Neon
Pooling:     Built-in (Supavisor/Neon proxy)
Search:      pg_search or built-in full-text
Vector:      pgvector (sufficient at this scale)
Analytics:   pg_duckdb for ad-hoc queries
Scheduling:  pg_cron
```

**For Scale-ups (1M-10M users):**
```
Database:    Self-managed or RDS/Aurora
Pooling:     PgCat (for query routing + replicas)
Search:      pg_search or dedicated Elasticsearch
Vector:      pgvector + pgvectorscale
Analytics:   Dedicated ClickHouse/BigQuery
Scheduling:  pg_cron + external orchestration
Partitioning: pg_partman for large tables
```

**For Enterprises (10M+ users):**
```
Database:    Citus (sharding) or CockroachDB (NewSQL)
Pooling:     Supavisor or PgCat cluster
Search:      Dedicated search infrastructure
Vector:      Dedicated vector DB (Milvus/Qdrant)
Analytics:   Data warehouse (Snowflake/BigQuery)
Real-time:   Kafka + streaming pipelines
```

---

## Summary: Key Principles for Senior/Staff Engineers

### 1. Design for the Access Patterns

Always start by understanding how data will be read and written. Schema design should optimize for actual workloads, not theoretical purity.

### 2. Constraints Are Your Friends

Use NOT NULL, UNIQUE, CHECK, and FOREIGN KEY constraints. Let the database enforce data integrity - don't rely solely on application code.

### 3. Profile Before Optimizing

Don't add indexes, denormalize, or optimize based on assumptions. Use EXPLAIN ANALYZE, query logs, and metrics to identify actual bottlenecks.

### 4. Plan for Change

- Use lookup tables for values that may change
- Consider how schema migrations will work
- Document why decisions were made

### 5. Understand Your Trade-offs

Every decision has trade-offs. Document them:
- Why did we denormalize this?
- Why did we choose UUID over auto-increment?
- Why did we use soft delete here?

### 6. Don't Over-Engineer

Start simple. Add complexity only when required by actual requirements or measured bottlenecks. Premature optimization is the root of all evil.

---

## References

### Schema Design & Best Practices
- [Database Schema Design for Scalability - DEV Community](https://dev.to/dhanush___b/database-schema-design-for-scalability-best-practices-techniques-and-real-world-examples-for-ida)
- [Top 10 Database Schema Design Best Practices - Bytebase](https://www.bytebase.com/blog/top-database-schema-design-best-practices/)
- [Normalization vs Denormalization Trade-offs - CelerData](https://celerdata.com/glossary/normalization-vs-denormalization-the-trade-offs-you-need-to-know)
- [Timestamps vs Booleans - PlanetScale](https://planetscale.com/learn/courses/mysql-for-developers/examples/timestamps-versus-booleans)
- [You Might As Well Timestamp It - Changelog](https://changelog.com/posts/you-might-as-well-timestamp-it)
- [Many-to-Many Relationships Guide - Beekeeper Studio](https://www.beekeeperstudio.io/blog/many-to-many-database-relationships-complete-guide)
- [Database Design for Audit Logging - Vertabelo](https://vertabelo.com/blog/database-design-for-audit-logging/)
- [OLTP vs OLAP - AWS](https://aws.amazon.com/compare/the-difference-between-olap-and-oltp/)
- [Lookup Table Best Practices - Apress](https://www.apress.com/gp/blog/all-blog-posts/best-practices-for-using-simple-lookup-tables/13323426)
- [UUID vs Auto-Increment - Bytebase](https://www.bytebase.com/blog/choose-primary-key-uuid-or-auto-increment/)
- [Soft Delete vs Hard Delete - HiBit](https://www.hibit.dev/posts/129/soft-deletes-vs-hard-deletes-making-the-right-choice)
- [Polymorphic Associations - GitLab Docs](https://docs.gitlab.com/ee/development/database/polymorphic_associations.html)
- [Temporal Tables - Microsoft Learn](https://learn.microsoft.com/en-us/sql/relational-databases/tables/temporal-tables)
- [Optimistic vs Pessimistic Locking - ByteByteGo](https://bytebytego.com/guides/pessimistic-vs-optimistic-locking/)
- [Database Indexing Strategies - ByteByteGo](https://blog.bytebytego.com/p/database-indexing-strategies)
- [Three-Valued Logic in SQL - LearnSQL](https://learnsql.com/blog/understanding-use-null-sql/)
- [Database Sharding Explained - MongoDB](https://www.mongodb.com/resources/products/capabilities/database-sharding-explained)
- [Database Anti-Patterns - InfoQ](https://www.infoq.com/articles/Anti-Patterns-Alois-Reitbauer/)
- [Rate Limiting Algorithms - GeeksforGeeks](https://www.geeksforgeeks.org/system-design/rate-limiting-algorithms-system-design/)

### Database Selection & Types
- [Database Selection Guide: SQL vs NoSQL vs NewSQL - DiNeuron](https://dineuron.com/database-selection-guide-sql-vs-nosql-vs-newsql-in-2025)
- [SQL vs NoSQL: Choosing the Right Database - ByteByteGo](https://blog.bytebytego.com/p/sql-vs-nosql-choosing-the-right-database)
- [SQL vs NoSQL: 5 Critical Differences - Integrate.io](https://www.integrate.io/blog/the-sql-vs-nosql-difference/)
- [SQL vs. NoSQL: Key Differences, Use Cases - Design Gurus](https://www.designgurus.io/blog/sql-vs-nosql-key-differences)

### Database Anti-Patterns
- [SQL Antipatterns: Avoiding the Pitfalls of Database Programming - Bill Karwin (Pragmatic Programmers)](https://pragprog.com/titles/bksqla/sql-antipatterns/) - **Primary reference for Section 17B**
- [SQL Antipatterns Volume 1 (Updated Edition) - Pragmatic Programmers](https://pragprog.com/titles/bksap1/sql-antipatterns-volume-1/)
- [SQL Antipatterns Book Summary - DEV Community](https://dev.to/treaz/book-summary-sql-antipatterns-1a5l)
- [An Overview of SQL Antipatterns - HackerNoon](https://hackernoon.com/an-overview-of-sql-antipatterns)
- [Database Anti-Patterns Part 2: Silent Killers - The Architect's Notebook](https://thearchitectsnotebook.substack.com/p/ep-67-database-anti-patterns-5-signs)
- [Common Database Design Patterns and Anti-Patterns - LinkedIn](https://www.linkedin.com/advice/1/what-some-most-common-database-design-patterns)
- [Database Modelization Anti-Patterns - Tapoueh](https://tapoueh.org/blog/2018/03/database-modelization-anti-patterns/)
- [Anti-Patterns in Database Design - Levitation](https://levitation.in/posts/anti-patterns-in-database-design-lessons-from-real-world-failures)
- [Busy Database Antipattern - Microsoft Azure](https://learn.microsoft.com/en-us/azure/architecture/antipatterns/busy-database/)

### Big Tech Database Architectures
- [Architecture of Giants: Data Stacks at Facebook, Netflix, Airbnb - Keen](https://blog.keen.io/architecture-of-giants-data-stacks-at-facebook-netflix-airbnb-and-pinterest/)
- [Netflix Technology Stack 2024 - Medium](https://medium.com/@romin991/in-depth-analysis-the-technology-stack-of-netflix-in-2024-443e12dc4b2a)
- [Netflix Cloud Architecture on AWS - Medium](https://medium.com/@ismailkovvuru/how-netflix-designed-its-global-cloud-architecture-on-aws-the-real-reason-netflix-moved-to-aws-0d823994ce46)
- [Big Data Techniques of Google, Amazon, Facebook, Twitter - ResearchGate](https://www.researchgate.net/publication/323588192_Review_Big_Data_Techniques_of_Google_Amazon_Facebook_and_Twitter)

### Vector Databases
- [Best Vector Databases in 2025 - Firecrawl](https://www.firecrawl.dev/blog/best-vector-databases-2025)
- [Vector Database Comparison 2025 - LiquidMetal AI](https://liquidmetal.ai/casesAndBlogs/vector-comparison/)
- [Choosing the Right Vector Database - Medium](https://medium.com/@elisheba.t.anderson/choosing-the-right-vector-database-opensearch-vs-pinecone-vs-qdrant-vs-weaviate-vs-milvus-vs-037343926d7e)
- [Top 10 Vector Databases for 2025 - Medium](https://medium.com/@bhagyarana80/top-10-vector-databases-for-2025-when-each-one-wins-fa2978b67650)
- [Best Vector Database for RAG 2025 - Digital One Agency](https://digitaloneagency.com.au/best-vector-database-for-rag-in-2025-pinecone-vs-weaviate-vs-qdrant-vs-milvus-vs-chroma/)
- [Milvus vs Other Vector Databases - Milvus.io](https://milvus.io/ai-quick-reference/how-does-milvus-compare-to-other-vector-databases-like-pinecone-or-weaviate)

### Modern & Analytical Databases
- [ClickHouse vs TimescaleDB vs InfluxDB 2025 Benchmarks](https://sanj.dev/post/clickhouse-timescaledb-influxdb-time-series-comparison)
- [Time-Series Databases 2025 Comparison - Markaicode](https://markaicode.com/time-series-databases-2025-comparison/)
- [ClickHouse vs DuckDB: Choosing the Right OLAP Database - Cloudraft](https://www.cloudraft.io/blog/clickhouse-vs-duckdb)
- [Databases in 2025: A Year in Review - Andy Pavlo (CMU)](https://www.cs.cmu.edu/~pavlo/blog/2026/01/2025-databases-retrospective.html)
- [OLAP Databases: What's New and Best in 2026 - Tinybird](https://www.tinybird.co/blog/best-database-for-olap)

### Graph Databases
- [Neo4j vs Amazon Neptune Comparison - Brilworks](https://www.brilworks.com/blog/neptune-vs-neo4j/)
- [Neo4j vs Amazon Neptune in Data Engineering - Analytics Vidhya](https://www.analyticsvidhya.com/blog/2024/08/neo4j-vs-amazon-neptune/)
- [When to Use a Graph Database - AWS Partner Network Blog](https://aws.amazon.com/blogs/apn/when-to-use-a-graph-database-like-neo4j-on-aws/)
- [AWS Neptune vs Neo4j: Which is Better? - PuppyGraph](https://www.puppygraph.com/blog/aws-neptune-vs-neo4j)

### Caching & In-Memory Databases
- [Redis vs Memcached: 7 Key Differences - Dragonfly](https://www.dragonflydb.io/guides/redis-vs-memcached-7-key-differences-and-how-to-choose)
- [Dragonfly: Modern Replacement for Redis - GitHub](https://github.com/dragonflydb/dragonfly)
- [Redis vs Dragonfly Scalability and Performance - Dragonfly](https://www.dragonflydb.io/blog/scaling-performance-redis-vs-dragonfly)
- [In-Memory Databases Comparison - Saif Rajhi Blog](https://seifrajhi.github.io/blog/in-memory-databases-comparison/)
- [Best Redis Alternatives - Dragonfly](https://www.dragonflydb.io/guides/best-redis-alternatives-top-oss-and-managed-solutions)

### Database Scaling
- [Scale Your Relational Database for SaaS - AWS](https://aws.amazon.com/blogs/database/scale-your-relational-database-for-saas-part-1-common-scaling-patterns/)
- [Database Scaling Patterns - Medium](https://medium.com/@aayushvlad/database-scaling-patterns-62ff9619efec)
- [Database Scaling Strategies - Codecademy](https://www.codecademy.com/article/database-scaling-strategies)
- [Top 15 Database Scaling Techniques - AlgoMaster](https://blog.algomaster.io/p/top-15-database-scaling-techniques)
- [Understanding Database Scaling Patterns - freeCodeCamp](https://www.freecodecamp.org/news/understanding-database-scaling-patterns)
- [Best Practices for Solving Database Scaling Problems - PingCAP](https://www.pingcap.com/article/best-practices-for-solving-database-scaling-problems/)

### Serverless PostgreSQL Platforms
- [Serverless PostgreSQL 2025: Supabase, Neon, PlanetScale - DEV Community](https://dev.to/dataformathub/serverless-postgresql-2025-the-truth-about-supabase-neon-and-planetscale-7lf)
- [Neon vs Supabase Comparison - Bytebase](https://www.bytebase.com/blog/neon-vs-supabase/)
- [PlanetScale vs Neon: MySQL vs PostgreSQL - Bytebase](https://www.bytebase.com/blog/planetscale-vs-neon/)
- [Neon vs PlanetScale vs Supabase - Bejamas](https://bejamas.com/compare/neon-vs-planetscale-vs-supabase)
- [Benchmarking Postgres - PlanetScale](https://planetscale.com/blog/benchmarking-postgres)

### Modern PostgreSQL Extensions
- [pgvectorscale: DiskANN for PostgreSQL - GitHub](https://github.com/timescale/pgvectorscale)
- [pgvectorscale Extension Overview - DbVisualizer](https://www.dbvis.com/thetable/pgvectorscale-an-extension-for-improved-vector-search-in-postgres/)
- [pgvector vs Pinecone at 75% Less Cost - TigerData](https://www.tigerdata.com/blog/pgvector-is-now-as-fast-as-pinecone-at-75-less-cost)
- [ParadeDB pg_search Documentation - ParadeDB](https://www.paradedb.com/)
- [ParadeDB 0.20.0 Release Notes](https://www.paradedb.com/blog/paradedb-0-20-0)
- [pg_analytics DuckDB-powered Analytics - GitHub](https://github.com/paradedb/pg_analytics)
- [pg_duckdb Extension - GitHub](https://github.com/duckdb/pg_duckdb)
- [pg_duckdb Beta Release - MotherDuck](https://motherduck.com/blog/pgduckdb-beta-release-duckdb-postgres/)
- [PostgreSQL and DuckDB Options - MotherDuck](https://motherduck.com/blog/postgres-duckdb-options/)
- [pg_cron Scheduling - GitHub](https://github.com/citusdata/pg_cron)
- [pg_cron on AWS RDS - Amazon](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/PostgreSQL_pg_cron.html)
- [pg_partman Partition Manager - PGXN](https://pgxn.org/dist/pg_partman/doc/pg_partman.html)
- [pg_graphql: GraphQL for PostgreSQL - Supabase](https://supabase.com/docs/guides/database/extensions/pg_graphql)
- [pg_jsonschema Documentation - Supabase GitHub](https://github.com/supabase/supabase/blob/master/apps/docs/content/guides/database/extensions/pg_jsonschema.mdx)
- [Supabase Postgres Extensions Overview](https://supabase.com/docs/guides/database/extensions)

### Connection Pooling
- [PgCat vs PgBouncer Comparison - pganalyze](https://pganalyze.com/blog/5mins-postgres-pgcat-vs-pgbouncer)
- [PostgreSQL Proxy Comparison 2025 - Onidel](https://onidel.com/blog/postgresql-proxy-comparison-2025)
- [Benchmarking PostgreSQL Connection Poolers - Tembo](https://legacy.tembo.io/blog/postgres-connection-poolers/)
- [PgCat: Modern Proxy for PostgreSQL - Medium](https://medium.com/codable/pgcat-the-modern-proxy-solution-for-postgresql-938a41755f91)
- [Adopting PgCat at Instacart - Tech Blog](https://tech.instacart.com/adopting-pgcat-a-nextgen-postgres-proxy-3cf284e68c2f)
- [Supavisor: Postgres Connection Pooler - GitHub](https://github.com/supabase/supavisor)
- [Supavisor 1.0 Release - Supabase](https://supabase.com/blog/supavisor-postgres-connection-pooler)
- [Supavisor: Scaling to 1 Million Connections - Supabase](https://supabase.com/blog/supavisor-1-million)

### Local-First & Edge Databases
- [ElectricSQL: Local-First Sync - GitHub](https://github.com/electric-sql/electric)
- [ElectricSQL Documentation](https://electric-sql.com/)
- [Local-First with TanStack DB - ElectricSQL](https://electric-sql.com/blog/2025/07/29/local-first-sync-with-tanstack-db)
- [PGlite: WASM PostgreSQL - GitHub](https://github.com/electric-sql/pglite)
- [PGlite Documentation](https://pglite.dev/)
- [Running PostgreSQL in Browser with WASM - InfoQ](https://www.infoq.com/news/2024/05/pglite-wasm-postgres-browser/)
- [FerretDB 2.0 Release - FerretDB Blog](https://blog.ferretdb.io/ferretdb-releases-v2-faster-more-compatible-mongodb-alternative/)
- [FerretDB: Open Source MongoDB Alternative - GitHub](https://github.com/FerretDB/FerretDB)
- [FerretDB 2.0 with PostgreSQL Power - The New Stack](https://thenewstack.io/ferretdb-2-0-open-source-mongodb-alternative-with-postgresql-power/)
- [FerretDB Cloud Announcement - InfoQ](https://www.infoq.com/news/2025/09/ferretdb-cloud-mongodb/)

### PostgreSQL Storage Engines
- [OrioleDB: Cloud-Native Storage Engine - GitHub](https://github.com/orioledb/orioledb)
- [OrioleDB Documentation](https://www.orioledb.com/docs)
- [OrioleDB Beta12 Benchmarks](https://www.orioledb.com/blog/orioledb-beta12-benchmarks)
- [OrioleDB on Supabase](https://supabase.com/docs/guides/database/orioledb)
- [Table Access Methods and Bloat - pganalyze](https://pganalyze.com/blog/5mins-postgres-bloat-table-access-methods)
- [Why PostgreSQL Needs Better Table Engine API - OrioleDB](https://www.orioledb.com/blog/better-table-access-methods)
- [Hydra: Serverless Analytics on Postgres](https://www.hydra.so/)
