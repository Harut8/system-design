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
- [Database Anti-Patterns Part 2: Silent Killers - The Architect's Notebook](https://thearchitectsnotebook.substack.com/p/ep-67-database-anti-patterns-5-signs)
- [Common Database Design Patterns and Anti-Patterns - LinkedIn](https://www.linkedin.com/advice/1/what-some-most-common-database-design-patterns)
- [Database Modelization Anti-Patterns - Tapoueh](https://tapoueh.org/blog/2018/03/database-modelization-anti-patterns/)
- [Anti-Patterns in Database Design - Levitation](https://levitation.in/posts/anti-patterns-in-database-design-lessons-from-real-world-failures)
- [Busy Database Antipattern - Microsoft Azure](https://learn.microsoft.com/en-us/azure/architecture/antipatterns/busy-database/)
- [SQL Antipatterns Book - Pragmatic Programmers](https://pragprog.com/titles/bksqla/sql-antipatterns/)

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
