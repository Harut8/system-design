# Query Engine Internals: A Deep Dive

How database query engines transform SQL text into efficient physical execution plans, schedule work across cores, and return results. This document covers the full pipeline from parsing through execution, with emphasis on the algorithms, data structures, and engineering trade-offs that separate production-grade engines from toy implementations.

---

## Table of Contents

1. [Query Processing Pipeline](#1-query-processing-pipeline)
2. [Parsing and Semantic Analysis](#2-parsing-and-semantic-analysis)
3. [Query Rewriting / Logical Optimization](#3-query-rewriting--logical-optimization)
4. [Cost-Based Query Optimization](#4-cost-based-query-optimization)
5. [Join Algorithms](#5-join-algorithms)
6. [Execution Models](#6-execution-models)
7. [Aggregation and Sorting](#7-aggregation-and-sorting)
8. [Parallel and Distributed Query Execution](#8-parallel-and-distributed-query-execution)
9. [Query Explain Plans](#9-query-explain-plans)

---

## 1. Query Processing Pipeline

### The Full Journey

Every SQL statement passes through a multi-stage pipeline before a single row is returned to the client. Each stage has a well-defined input and output, and each stage can reject the query with an error.

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    QUERY PROCESSING PIPELINE                            │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│   SQL Text                                                              │
│     │                                                                   │
│     ▼                                                                   │
│  ┌──────────────┐                                                       │
│  │   LEXER      │  "SELECT id FROM users WHERE age > 21"                │
│  │  (Tokenizer) │  → [SELECT] [id] [FROM] [users] [WHERE] [age] [>]... │
│  └──────┬───────┘                                                       │
│         │  Token Stream                                                 │
│         ▼                                                               │
│  ┌──────────────┐                                                       │
│  │   PARSER     │  Tokens → Abstract Syntax Tree (AST)                  │
│  │  (Grammar)   │  Validates SQL syntax, builds tree structure          │
│  └──────┬───────┘                                                       │
│         │  Raw AST                                                      │
│         ▼                                                               │
│  ┌──────────────┐                                                       │
│  │  ANALYZER    │  Name resolution, type checking, view expansion       │
│  │  (Semantic)  │  Resolves tables → catalog, columns → schemas         │
│  └──────┬───────┘                                                       │
│         │  Annotated / Resolved AST                                     │
│         ▼                                                               │
│  ┌──────────────┐                                                       │
│  │  REWRITER    │  Logical transformations (predicate pushdown,         │
│  │  (Logical)   │  constant folding, subquery unnesting, etc.)          │
│  └──────┬───────┘                                                       │
│         │  Optimized Logical Plan                                       │
│         ▼                                                               │
│  ┌──────────────┐                                                       │
│  │  OPTIMIZER   │  Cost-based search: join ordering, access paths,      │
│  │  (Physical)  │  join algorithms, parallelism decisions               │
│  └──────┬───────┘                                                       │
│         │  Physical Execution Plan                                      │
│         ▼                                                               │
│  ┌──────────────┐                                                       │
│  │  EXECUTOR    │  Volcano iterator, vectorized batch, or compiled      │
│  │  (Runtime)   │  code execution. Manages memory, spill, parallelism   │
│  └──────┬───────┘                                                       │
│         │  Result Rows / Batches                                        │
│         ▼                                                               │
│  ┌──────────────┐                                                       │
│  │  TRANSPORT   │  Serialize results (wire protocol), send to client    │
│  │  (Network)   │  PostgreSQL: row-at-a-time, MySQL: row-at-a-time     │
│  └──────────────┘                                                       │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### Stage Responsibilities Summary

| Stage | Input | Output | Can Fail With |
|-------|-------|--------|---------------|
| Lexer | SQL text | Token stream | Syntax error (bad characters) |
| Parser | Token stream | Raw AST | Syntax error (grammar violation) |
| Analyzer | Raw AST + Catalog | Annotated AST | Unknown table/column, type mismatch |
| Rewriter | Annotated AST | Optimized logical plan | (Rarely fails) |
| Optimizer | Logical plan + Statistics | Physical plan | Timeout (falls back to heuristic) |
| Executor | Physical plan | Result set | OOM, disk full, lock timeout, etc. |
| Transport | Result set | Wire bytes | Connection reset, client disconnect |

### Pipeline Variations Across Systems

- **PostgreSQL**: Classic pipeline. Parser produces a parse tree (not AST), which the analyzer converts to a Query tree. The planner/optimizer is a single combined stage. No JIT by default (optional LLVM JIT for expressions since PG 11).
- **MySQL**: Parser produces a tree, then the optimizer does both logical and physical optimization in one pass. Historically had a very simple optimizer; 8.0 added hash joins, histograms, and a proper cost model.
- **DuckDB**: Uses a Binder (analyzer) that resolves names, then a logical planner, then a physical planner with vectorized execution.
- **CockroachDB**: Parser (Go-based) produces AST, then optbuilder constructs a normalized expression tree, then the Cascades-style optimizer (called "opt") searches for the best physical plan.

---

## 2. Parsing and Semantic Analysis

### 2.1 Lexical Analysis (Tokenization)

The lexer (scanner/tokenizer) reads raw SQL text character-by-character and produces a stream of tokens. Each token has a type and a value.

```
Input:  SELECT name, age FROM users WHERE age >= 21 AND status = 'active'

Tokens:
  ┌───────────┬──────────┬───────────────┐
  │  Position │   Type   │     Value     │
  ├───────────┼──────────┼───────────────┤
  │    0      │ KEYWORD  │ SELECT        │
  │    7      │ IDENT    │ name          │
  │   11      │ COMMA    │ ,             │
  │   13      │ IDENT    │ age           │
  │   17      │ KEYWORD  │ FROM          │
  │   22      │ IDENT    │ users         │
  │   28      │ KEYWORD  │ WHERE         │
  │   34      │ IDENT    │ age           │
  │   38      │ OP       │ >=            │
  │   41      │ INTEGER  │ 21            │
  │   44      │ KEYWORD  │ AND           │
  │   48      │ IDENT    │ status        │
  │   55      │ OP       │ =             │
  │   57      │ STRING   │ 'active'      │
  └───────────┴──────────┴───────────────┘
```

Key lexer challenges:

- **Keyword vs Identifier ambiguity**: Is `value` a keyword or a column name? Most databases maintain a reserved word list, and allow non-reserved keywords as identifiers based on context.
- **Quoted identifiers**: `"order"` (double-quoted in PostgreSQL/standard SQL) vs `` `order` `` (backtick-quoted in MySQL) allows reserved words as identifiers.
- **String escaping**: Standard SQL uses `''` for literal single quotes inside strings. PostgreSQL supports `E'...'` for C-style escapes. MySQL supports `\"` with `BACKSLASH_ESCAPES` mode.
- **Multi-byte characters**: UTF-8 identifiers, emoji in string literals, etc.

### 2.2 Parsing and the Abstract Syntax Tree

The parser consumes the token stream and produces an Abstract Syntax Tree (AST) according to the SQL grammar. Most production databases use hand-written recursive descent parsers (PostgreSQL, CockroachDB) or parser generators like Bison/Yacc (MySQL, older PostgreSQL versions).

**Simplified BNF-like grammar for SELECT:**

```
<select_stmt>   ::= SELECT <select_list> FROM <from_clause>
                     [ WHERE <expr> ]
                     [ GROUP BY <expr_list> ]
                     [ HAVING <expr> ]
                     [ ORDER BY <order_list> ]
                     [ LIMIT <integer> [ OFFSET <integer> ] ]

<select_list>   ::= '*'
                   | <select_item> ( ',' <select_item> )*

<select_item>   ::= <expr> [ [ AS ] <alias> ]

<from_clause>   ::= <table_ref> ( ',' <table_ref> )*
                   | <table_ref> <join_clause>*

<join_clause>   ::= [ INNER | LEFT | RIGHT | FULL | CROSS ] JOIN
                     <table_ref> [ ON <expr> | USING '(' <column_list> ')' ]

<table_ref>     ::= <table_name> [ [ AS ] <alias> ]
                   | '(' <select_stmt> ')' [ AS ] <alias>
                   | <table_name> [ [ AS ] <alias> ] '(' <expr_list> ')'

<expr>          ::= <expr> <binary_op> <expr>
                   | <unary_op> <expr>
                   | <expr> IS [ NOT ] NULL
                   | <expr> [ NOT ] IN '(' <expr_list> ')'
                   | <expr> [ NOT ] IN '(' <select_stmt> ')'
                   | <expr> [ NOT ] BETWEEN <expr> AND <expr>
                   | <expr> [ NOT ] LIKE <expr>
                   | EXISTS '(' <select_stmt> ')'
                   | <function_call>
                   | <column_ref>
                   | <literal>
                   | '(' <expr> ')'
                   | '(' <select_stmt> ')'       -- scalar subquery
                   | CASE <case_expr> END

<column_ref>    ::= [ <table_name> '.' ] <column_name>

<binary_op>     ::= '+' | '-' | '*' | '/' | '=' | '<>' | '!='
                   | '<' | '>' | '<=' | '>=' | AND | OR

<literal>       ::= <integer> | <float> | <string> | NULL | TRUE | FALSE
```

**Resulting AST for a query:**

```sql
SELECT u.name, COUNT(o.id) AS order_count
FROM users u
JOIN orders o ON u.id = o.user_id
WHERE u.status = 'active'
GROUP BY u.name
HAVING COUNT(o.id) > 5
ORDER BY order_count DESC
LIMIT 10
```

```
                        SelectStmt
                       /    |     \
                      /     |      \
               SelectList  From    Where
               /      \      |        \
           ColRef   FuncCall JoinExpr   BinOp(=)
           u.name   COUNT    /  |  \    /     \
                    (o.id)  /   |   \ ColRef  Literal
                          /    |    \ u.status 'active'
                      TableRef ON  TableRef
                      users(u) |   orders(o)
                           BinOp(=)
                           /      \
                       ColRef    ColRef
                       u.id     o.user_id
                      ─────────────────
                      GroupBy: [u.name]
                      Having:  BinOp(>) { COUNT(o.id), 5 }
                      OrderBy: [order_count DESC]
                      Limit:   10
```

### 2.3 Semantic Analysis (The Analyzer / Binder)

The analyzer (also called the binder or semantic analyzer) takes the raw AST and resolves all names, checks types, and expands views. This is where the system catalog (metadata) is consulted.

**Name Resolution Process:**

```
┌─────────────────────────────────────────────────────────────┐
│                    NAME RESOLUTION                           │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  1. Resolve table names                                     │
│     "users" → schema "public", table OID 16384              │
│     "orders" → schema "public", table OID 16390             │
│                                                             │
│  2. Resolve aliases                                         │
│     "u" → users (OID 16384)                                 │
│     "o" → orders (OID 16390)                                │
│                                                             │
│  3. Resolve column names                                    │
│     "u.name"    → users.name    (type: varchar, nullable)   │
│     "u.id"      → users.id      (type: integer, NOT NULL)   │
│     "u.status"  → users.status   (type: varchar, nullable)  │
│     "o.id"      → orders.id      (type: integer, NOT NULL)  │
│     "o.user_id" → orders.user_id (type: integer, nullable)  │
│                                                             │
│  4. Resolve unqualified columns                             │
│     If "name" appears without table qualifier:              │
│     → Search all tables in FROM clause                      │
│     → If found in exactly one table: resolve                │
│     → If found in multiple tables: ERROR (ambiguous)        │
│     → If found in zero tables: ERROR (unknown column)       │
│                                                             │
│  5. Resolve output aliases                                  │
│     "order_count" in ORDER BY → refers to                   │
│     the SELECT-list alias COUNT(o.id) AS order_count        │
│     (PostgreSQL allows this; standard SQL does not)         │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

**Type Checking and Coercion:**

The analyzer ensures that all operations have type-compatible operands. When types do not match exactly, implicit coercion rules apply.

```
Type coercion hierarchy (PostgreSQL-style):

  boolean
  smallint → integer → bigint → numeric → float4 → float8
  date → timestamp → timestamptz
  char → varchar → text

Coercion examples:
  integer + float8       → float8 + float8       (widen integer to float8)
  varchar = integer      → ERROR or implicit cast (depends on system)
  date > '2024-01-01'    → date > date            (parse string as date literal)
  integer IN (1,2,'3')   → integer IN (1,2,3)     (coerce '3' to integer)
```

**Common type-checking errors:**

```sql
-- ERROR: operator does not exist: integer = text
SELECT * FROM users WHERE id = 'abc';

-- OK with implicit cast (PostgreSQL): string parsed as integer
SELECT * FROM users WHERE id = '42';

-- ERROR: argument of WHERE must be boolean
SELECT * FROM users WHERE name;

-- ERROR: aggregate functions not allowed in WHERE
SELECT * FROM users WHERE COUNT(*) > 5;

-- OK: aggregate in HAVING (post-GROUP BY filter)
SELECT dept, COUNT(*) FROM users GROUP BY dept HAVING COUNT(*) > 5;
```

### 2.4 View Expansion

When a query references a view, the analyzer replaces the view reference with the view's stored SELECT statement, then re-analyzes the combined query.

```sql
-- View definition stored in catalog:
CREATE VIEW active_users AS
  SELECT id, name, email FROM users WHERE status = 'active';

-- User query:
SELECT name FROM active_users WHERE email LIKE '%@company.com';

-- After view expansion (what the optimizer actually sees):
SELECT name FROM (
  SELECT id, name, email FROM users WHERE status = 'active'
) AS active_users
WHERE email LIKE '%@company.com';

-- After predicate merging (rewriter optimization):
SELECT name FROM users
WHERE status = 'active' AND email LIKE '%@company.com';
```

This last step (merging predicates from the outer query into the view's subquery) is a rewriter optimization, covered next. The view expansion itself is purely mechanical substitution.

---

## 3. Query Rewriting / Logical Optimization

The rewriter applies semantics-preserving transformations to the logical plan. These transformations do not depend on statistics or cost estimates -- they are always beneficial (or at worst neutral). The goal is to reduce the search space for the cost-based optimizer and to enable physical optimizations.

### 3.1 Predicate Pushdown

Move filter conditions as close to the data source as possible. This is the single most impactful logical optimization.

```
BEFORE pushdown:                    AFTER pushdown:

    Filter                              Join
  age > 21                            /     \
      |                              /       \
    Join                         Filter     Scan
   /    \                       age > 21   orders
  /      \                         |
Scan    Scan                     Scan
users   orders                   users
```

**Rules for predicate pushdown:**

| Predicate Location | Can Push Through | Cannot Push Through |
|--------------------|-----------------|--------------------|
| After INNER JOIN | Into either child if columns match | OUTER side of LEFT/RIGHT JOIN |
| After LEFT JOIN (on preserved side) | Into the preserved (left) child | Into the null-supplying (right) child |
| After GROUP BY | Only if predicate uses grouping columns | If predicate uses aggregates (stays as HAVING) |
| After DISTINCT | Yes, always safe | (None) |
| After UNION ALL | Into both branches | Into UNION (with DISTINCT) unless careful |
| After LIMIT | Generally no (changes semantics) | Most cases |

**Example with LEFT JOIN complication:**

```sql
-- Original:
SELECT u.name, o.total
FROM users u
LEFT JOIN orders o ON u.id = o.user_id
WHERE o.total > 100;

-- The WHERE clause on o.total effectively converts this to an INNER JOIN,
-- because NULL (from unmatched left rows) will not satisfy > 100.
-- Optimizer recognizes this and can rewrite to:

SELECT u.name, o.total
FROM users u
INNER JOIN orders o ON u.id = o.user_id
WHERE o.total > 100;

-- Now predicate pushdown can push o.total > 100 into orders scan.
```

### 3.2 Constant Folding

Evaluate constant expressions at compile time rather than per-row at runtime.

```sql
-- Before constant folding:
SELECT * FROM events WHERE event_date > CURRENT_DATE - INTERVAL '30 days';

-- After constant folding (assuming today is 2024-06-15):
SELECT * FROM events WHERE event_date > '2024-05-16';

-- Arithmetic folding:
WHERE price * 1.1 > 100    →   WHERE price > 90.909...
-- Note: the optimizer may OR MAY NOT do this transformation.
-- Rewriting "price * 1.1 > 100" to "price > 100/1.1" allows index usage on price,
-- but is only valid for monotonic functions. Division by zero, overflow, and
-- floating-point precision must be considered.

-- Boolean folding:
WHERE TRUE AND age > 21    →   WHERE age > 21
WHERE FALSE OR age > 21   →   WHERE age > 21
WHERE FALSE AND age > 21  →   (entire query returns empty -- can short-circuit)
```

### 3.3 Subquery Unnesting / Decorrelation

Correlated subqueries are a major performance problem because the naive execution strategy evaluates the subquery once per row of the outer query. Decorrelation transforms them into joins.

```sql
-- Correlated subquery (O(n*m) naive execution):
SELECT u.name
FROM users u
WHERE u.salary > (
    SELECT AVG(salary) FROM users u2 WHERE u2.dept_id = u.dept_id
);

-- After decorrelation (rewritten as a join):
SELECT u.name
FROM users u
JOIN (
    SELECT dept_id, AVG(salary) AS avg_salary
    FROM users
    GROUP BY dept_id
) dept_avg ON u.dept_id = dept_avg.dept_id
WHERE u.salary > dept_avg.avg_salary;
```

```
BEFORE decorrelation:              AFTER decorrelation:

  Filter                                Join (u.salary > avg_salary)
  u.salary > SubQuery                  /          \
     |                                /            \
  Scan(users u)                   Scan            GroupBy(dept_id)
     |                           users u          AVG(salary)
  [for each row, execute:]                            |
     GroupBy(AVG)                                  Scan
        |                                         users u2
     Filter(u2.dept_id = u.dept_id)
        |
     Scan(users u2)
```

**Common unnesting patterns:**

| Pattern | Rewrite |
|---------|---------|
| `WHERE x IN (SELECT ...)` | Semi-join |
| `WHERE x NOT IN (SELECT ...)` | Anti-join (careful with NULLs!) |
| `WHERE EXISTS (SELECT ...)` | Semi-join |
| `WHERE NOT EXISTS (SELECT ...)` | Anti-join |
| `WHERE x > (SELECT agg FROM ... WHERE correlated)` | Join with derived table |
| `SELECT (SELECT ... WHERE correlated)` in select list | Left join with derived table |

**NULL semantics trap with NOT IN:**

```sql
-- NOT IN with NULLs is treacherous:
-- If the subquery returns {1, 2, NULL}, then:
--   3 NOT IN (1, 2, NULL) → UNKNOWN (not TRUE!)
-- This means NO rows satisfy the NOT IN condition if any NULL exists.

-- NOT EXISTS does not have this problem:
-- EXISTS checks for row existence, not value equality.

-- Therefore: WHERE x NOT IN (SELECT y ...) is NOT equivalent to
-- WHERE NOT EXISTS (SELECT 1 ... WHERE y = x) when NULLs are possible.
-- The optimizer must be careful during this rewrite.
```

### 3.4 Join Elimination

Remove joins that cannot affect the query result. This happens more often than you might expect, especially with views and ORMs.

```sql
-- Join to a table whose columns are not used and the join is guaranteed
-- to produce exactly one match per row (foreign key + unique constraint):

SELECT u.name
FROM users u
JOIN departments d ON u.dept_id = d.id;
-- If d.id is the primary key of departments AND u.dept_id has a
-- NOT NULL foreign key constraint to d.id, then every user row
-- matches exactly one department row, and no department columns
-- are selected. The join can be eliminated:
→ SELECT u.name FROM users u;

-- This commonly happens with ORM-generated queries that eagerly join
-- tables but only select columns from one side.
```

**Requirements for join elimination:**

1. No columns from the eliminated table appear in SELECT, WHERE, ORDER BY, GROUP BY, or HAVING.
2. The join is provably non-duplicating (unique/PK on the join column of the eliminated side).
3. The join is provably non-filtering (NOT NULL FK constraint or LEFT JOIN).

### 3.5 Partition Pruning

For partitioned tables, eliminate partitions that cannot contain matching rows.

```sql
-- Table partitioned by month:
-- events_2024_01, events_2024_02, ..., events_2024_12

SELECT * FROM events
WHERE event_date BETWEEN '2024-03-01' AND '2024-03-31';

-- Partition pruning: only scan events_2024_03
-- Skip all other 11 partitions entirely

-- Dynamic partition pruning (determined at runtime):
SELECT * FROM events e
JOIN date_filter d ON e.event_date = d.filter_date;
-- If the optimizer builds the hash table on date_filter first,
-- it can determine which partitions to read from events at runtime.
```

### 3.6 Common Subexpression Elimination

Identify identical subexpressions and compute them once.

```sql
-- Before CSE:
SELECT
    EXTRACT(YEAR FROM created_at) AS year,
    EXTRACT(MONTH FROM created_at) AS month,
    COUNT(*) FILTER (WHERE EXTRACT(YEAR FROM created_at) = 2024)
FROM orders
GROUP BY EXTRACT(YEAR FROM created_at), EXTRACT(MONTH FROM created_at);

-- After CSE: compute EXTRACT(YEAR FROM created_at) once, reuse the result.
-- The executor assigns the result to a temporary slot and references it.
```

This is more impactful when the repeated expression is expensive (user-defined functions, complex CASE expressions, regex operations).

### 3.7 OR to UNION Transformation

An OR condition on different columns cannot use a single index. Converting to UNION allows each branch to use its own index.

```sql
-- Before: full table scan (no single index covers both columns)
SELECT * FROM users
WHERE email = 'alice@example.com' OR phone = '555-0100';

-- After OR-to-UNION:
SELECT * FROM users WHERE email = 'alice@example.com'
UNION
SELECT * FROM users WHERE phone = '555-0100';

-- Now each branch can use its respective index:
-- Branch 1: Index Scan on idx_users_email
-- Branch 2: Index Scan on idx_users_phone
-- UNION removes duplicates (rows matching both conditions).
-- If duplicates are impossible, UNION ALL is used (cheaper).
```

**When this is not beneficial:** If the table is small or both predicates have low selectivity (match many rows), sequential scan is cheaper than two index scans + dedup.

---

## 4. Cost-Based Query Optimization

The cost-based optimizer (CBO) is the heart of any serious query engine. It explores the space of possible physical plans and selects the one with the lowest estimated cost. This is where most of the complexity (and most of the bugs) live.

### 4.1 The Optimizer's Search Space

For a given logical plan, the optimizer must choose:

```
┌─────────────────────────────────────────────────────────────────┐
│                     OPTIMIZER DECISIONS                          │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  1. ACCESS PATH for each table:                                 │
│     ├── Sequential scan                                         │
│     ├── Index scan (which index? forward/backward?)             │
│     ├── Index-only scan (covers all needed columns)             │
│     ├── Bitmap index scan (combine multiple indexes)            │
│     └── TID scan (direct tuple access)                          │
│                                                                 │
│  2. JOIN ORDER: In what sequence to join N tables?              │
│     ├── Left-deep tree (only left-deep plans)                   │
│     ├── Right-deep tree                                         │
│     ├── Bushy tree (any shape)                                  │
│     └── For N tables: N! orderings (left-deep), Catalan         │
│         number of bushy trees                                   │
│                                                                 │
│  3. JOIN ALGORITHM for each join:                               │
│     ├── Nested Loop Join (+ index lookup on inner)              │
│     ├── Hash Join (build hash table on smaller side)            │
│     ├── Sort-Merge Join (sort both, merge)                      │
│     └── (Distributed: broadcast join, shuffle join)             │
│                                                                 │
│  4. AGGREGATION STRATEGY:                                       │
│     ├── Hash aggregation (build hash table on GROUP BY keys)    │
│     ├── Sort-based aggregation (sort, then scan groups)         │
│     └── (If already sorted by GROUP BY keys: streaming agg)     │
│                                                                 │
│  5. PARALLELISM:                                                │
│     ├── Sequential (single worker)                              │
│     ├── Intra-operator parallelism (partition one operator)     │
│     └── Inter-operator parallelism (pipeline operators)         │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 4.2 Cardinality Estimation

Cardinality estimation is the process of predicting how many rows each operator will produce. It is the most important (and most error-prone) component of cost-based optimization. If cardinality estimates are wrong, the optimizer will choose a bad plan.

**Why it matters:**

```
Estimated rows: 100    → Optimizer chooses: Nested Loop with Index Lookup
Actual rows:  1,000,000 → Correct choice:   Hash Join

A 10,000x estimation error leads to a plan that is potentially
100,000x slower (index lookup per row vs. single hash build + probe).
```

#### Histograms

Histograms capture the distribution of values in a column. There are two main types:

**Equi-Width Histogram:**

```
Column: age (values 18-80)
Bucket width: 10 years

Bucket    Range       Frequency
  1       [18, 28)      4,500
  2       [28, 38)     12,000
  3       [38, 48)     15,000
  4       [48, 58)      8,000
  5       [58, 68)      3,500
  6       [68, 78)      1,800
  7       [78, 88)        200

Total:                 45,000

Selectivity estimate for age >= 50:
  Bucket 4 partial: (58-50)/(58-48) * 8000 = 6,400
  Bucket 5 full:    3,500
  Bucket 6 full:    1,800
  Bucket 7 full:      200
  Estimate:         11,900 / 45,000 ≈ 26.4%
```

Problem: Equi-width histograms are inaccurate for skewed distributions (common in real data).

**Equi-Depth (Equi-Height) Histogram:**

```
Column: age (45,000 rows, 200 buckets, ~225 rows each)
Each bucket contains approximately the same number of rows.

Bucket    Range       Frequency    Distinct Values
  1       [18, 22)       225           5
  2       [22, 24)       225           3  ← narrow bucket = dense region
  3       [24, 25)       225           2  ← very narrow = very dense
  ...
 150      [55, 62)       225          8   ← wide bucket = sparse region
  ...
 200      [77, 80]       225          4

Selectivity estimate for age = 25:
  Find bucket containing 25: bucket 4, range [25, 27), 225 rows, 3 distinct values
  Estimate: 225 / 3 = 75 rows
  (Assumes uniform distribution within bucket)
```

PostgreSQL uses equi-depth histograms with 100 buckets by default (`default_statistics_target = 100`). You can increase this per-column:

```sql
ALTER TABLE users ALTER COLUMN age SET STATISTICS 1000;
ANALYZE users;
```

#### Most Common Values (MCV)

For columns with a few very frequent values (e.g., `status` column with values `active`, `inactive`, `pending`), histograms are less effective. Instead, databases store the N most common values and their frequencies separately.

```sql
-- PostgreSQL stores MCVs in pg_stats:
SELECT most_common_vals, most_common_freqs
FROM pg_stats
WHERE tablename = 'users' AND attname = 'status';

-- Result:
-- most_common_vals:  {active, inactive, pending, banned}
-- most_common_freqs: {0.72, 0.18, 0.08, 0.02}

-- Selectivity of status = 'active': 0.72 (directly from MCV list)
-- Selectivity of status = 'unknown': (1 - sum(MCV_freqs)) / (n_distinct - len(MCVs))
--                                   = (1 - 1.0) / ... ≈ very small
```

#### Distinct Value Estimation (NDV)

The number of distinct values in a column is crucial for join cardinality estimation.

```
Methods for estimating NDV:

1. Exact count (ANALYZE does a full scan or sample):
   SELECT COUNT(DISTINCT column) FROM table;
   Expensive for large tables.

2. HyperLogLog (probabilistic):
   Approximate NDV with ~2% error using O(1) memory.
   PostgreSQL uses a sampling-based approach instead.

3. Sample-based estimation:
   Take a random sample of S rows.
   Count distinct values d_s in the sample.
   Estimate total NDV using Haas-Stokes estimator or similar.

4. PostgreSQL approach:
   ANALYZE samples ~30,000 rows (300 * default_statistics_target).
   Computes n_distinct from the sample.
   Stores as positive number (exact count) or negative fraction
   (-0.5 means "50% of rows are distinct").
```

#### Why Cardinality Estimation Is Often Wrong

```
┌─────────────────────────────────────────────────────────────────┐
│            SOURCES OF ESTIMATION ERROR                           │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  1. INDEPENDENCE ASSUMPTION                                     │
│     P(A AND B) = P(A) * P(B)                                   │
│     Real data has correlations!                                 │
│     Example: city = 'San Francisco' AND state = 'CA'            │
│     Estimate: 0.01 * 0.08 = 0.0008 (way too low)               │
│     Actual:   0.01 (same as just city, since city implies state) │
│                                                                 │
│  2. UNIFORMITY ASSUMPTION WITHIN BUCKETS                        │
│     Histogram bucket [100, 200): 1000 rows                      │
│     Estimate for value = 150: 1000/100 = 10                     │
│     Actual: value 150 might have 500 rows (hot spot)            │
│                                                                 │
│  3. JOIN CARDINALITY ERRORS COMPOUND                            │
│     If each join has 2x error:                                  │
│     After 5 joins: 2^5 = 32x error                              │
│     After 10 joins: 2^10 = 1024x error                          │
│     This is why queries with many joins are hardest to optimize. │
│                                                                 │
│  4. STALE STATISTICS                                            │
│     Data changes between ANALYZE runs.                          │
│     Table had 1M rows when analyzed, now has 10M rows.          │
│     All estimates off by 10x.                                   │
│                                                                 │
│  5. FUNCTION SELECTIVITY                                        │
│     WHERE my_function(column) = 'value'                         │
│     Optimizer has no statistics for function output.             │
│     Falls back to magic constants (0.33 for equality, etc.)     │
│                                                                 │
│  6. MISSING STATISTICS                                          │
│     Foreign tables, CTEs, complex subqueries.                   │
│     Optimizer guesses (often 0.1% selectivity).                 │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

**Mitigations for estimation errors:**

- **Multi-column statistics** (PostgreSQL 10+): `CREATE STATISTICS` for correlated columns.
- **Adaptive execution** (discussed in 4.7): Re-optimize mid-query when actual cardinalities diverge from estimates.
- **Plan hints** (Oracle, MySQL, SQL Server): Override the optimizer's choices.
- **Plan stability** (baselines, pinned plans): Prevent plan regressions.

### 4.3 Cost Models

The cost model assigns a numeric cost to each physical operator. The optimizer compares total plan costs and picks the cheapest.

```
Cost(plan) = CPU_cost + IO_cost + Network_cost (distributed only)

PostgreSQL cost model parameters (postgresql.conf):

  seq_page_cost    = 1.0    -- cost of reading one sequential page
  random_page_cost = 4.0    -- cost of reading one random page (seek + read)
  cpu_tuple_cost   = 0.01   -- cost of processing one tuple
  cpu_index_tuple_cost = 0.005 -- cost of processing one index entry
  cpu_operator_cost = 0.0025  -- cost of one operator evaluation
  effective_cache_size = 4GB  -- planner's assumption about OS cache

Example cost calculation for a sequential scan:

  Table: users (10,000 pages, 1,000,000 rows)
  Filter: age > 50 (selectivity = 0.3)

  IO_cost  = 10,000 pages * seq_page_cost(1.0) = 10,000
  CPU_cost = 1,000,000 rows * cpu_tuple_cost(0.01)
           + 1,000,000 rows * cpu_operator_cost(0.0025)  -- evaluate age > 50
           = 10,000 + 2,500 = 12,500
  Total    = 22,500

Example cost calculation for an index scan:

  Index: idx_users_age (B-tree)
  Matching rows: 300,000 (selectivity 0.3)

  Index IO  = 300,000 * random_page_cost(4.0) * (1 - cache_hit_ratio)
  -- But many index pages and heap pages will be cached...
  -- PostgreSQL uses a sophisticated model that estimates cache hit ratio
  -- based on effective_cache_size and table/index size.

  If most pages are cached (table fits in memory):
    Index scan cost ≈ 300,000 * 0.005 (cpu) ≈ 1,500
  If table is much larger than cache:
    Index scan cost ≈ 300,000 * 4.0 = 1,200,000 (much worse than seq scan!)
```

**Key insight**: Index scans are not always faster. For low selectivity (many matching rows), sequential scan wins because sequential I/O is much cheaper than random I/O. The crossover point depends on cache hit ratio and is typically around 5-15% selectivity for disk-bound tables.

```
Cost vs Selectivity:

Cost │
     │  Index Scan
     │  (random I/O)
     │        /
     │       /
     │      /
     │     /
     │    /        ───────────── Sequential Scan (constant cost)
     │   /        /
     │  /        /
     │ /   ─────/
     │/ ──/
     ├────────────────────────── Selectivity
     0%    5%   15%   50%  100%
          ↑
          Crossover point
          (system-dependent)
```

### 4.4 Dynamic Programming for Join Ordering (Selinger-Style)

The seminal System R paper by Selinger et al. (1979) introduced the dynamic programming approach to join ordering, which is still the foundation of most modern optimizers.

**The Problem:** For N tables, there are N! possible left-deep join orderings and exponentially more bushy orderings. Exhaustive enumeration is infeasible for large N.

**The Insight:** Optimal plans for subsets of tables can be combined to form optimal plans for larger subsets. This is the principle of optimality in dynamic programming.

```
Algorithm: Selinger Dynamic Programming for Join Ordering

Input: Set of N tables {R1, R2, ..., Rn}, join predicates, statistics
Output: Optimal join order and algorithms

Phase 1: Single-table access paths
  For each table Ri:
    Enumerate access paths: seq scan, index scans, bitmap scans
    Keep the cheapest, PLUS any plan with an "interesting order"
    (sorted on a join column or ORDER BY column)

Phase 2: Two-table joins
  For each pair (Ri, Rj) that can be joined:
    For each combination of access paths for Ri and Rj:
      For each join algorithm (NLJ, hash, merge):
        Compute cost
    Keep cheapest plan for {Ri, Rj}, plus interesting orders

Phase 3: Three-table joins
  For each triple {Ri, Rj, Rk}:
    Try: best({Ri, Rj}) JOIN Rk
    Try: best({Ri, Rk}) JOIN Rj
    Try: best({Rj, Rk}) JOIN Ri
    For each: try all join algorithms, keep cheapest + interesting orders

...continue until all N tables are joined...

Phase N: Final plan
  The cheapest plan for {R1, R2, ..., Rn} is the answer.
  If ORDER BY is required, also consider plans with matching order
  (avoid a separate Sort operator).
```

**"Interesting Orders" -- the key insight:**

```
Consider a query:
  SELECT * FROM A JOIN B ON A.x = B.x JOIN C ON B.y = C.y ORDER BY A.x;

Plan 1: Hash Join(Hash Join(SeqScan A, SeqScan B), SeqScan C) + Sort
  Cost: 1000 (joins) + 500 (sort) = 1500

Plan 2: MergeJoin(IndexScan A on x, Sort(SeqScan B)) + Hash Join C
  Cost: 1200 (joins) + 0 (already sorted!) = 1200

Plan 2 is more expensive for the joins alone but avoids the final sort.
Without tracking "interesting orders," the optimizer would discard Plan 2
after Phase 2 (because the Hash Join plan was cheaper for {A,B}).
```

**Complexity:**

| Approach | Time | Space |
|----------|------|-------|
| Left-deep only | O(N! * access_paths) | O(2^N) |
| Bushy trees | O(3^N) with DP | O(2^N) |
| Left-deep with DP | O(N * 2^N) | O(2^N) |

For N > 10-12 tables, even DP becomes too slow. Solutions:

- **Genetic Query Optimizer (GEQO)**: PostgreSQL uses a genetic algorithm for queries joining more than `geqo_threshold` tables (default: 12).
- **Randomized algorithms**: Simulate annealing, iterative improvement.
- **Greedy heuristics**: Always join the two cheapest relations next.
- **Cascades/Volcano optimizer**: Memo-based top-down search with pruning (used by SQL Server, CockroachDB).

### 4.5 The Plan Space Explosion Problem

```
Number of possible join orderings:

Tables (N)  │ Left-Deep  │ Bushy Trees      │ With 3 Join Algos
────────────┼────────────┼──────────────────┼───────────────────
     3      │      6     │       12         │        324
     5      │    120     │     1,680        │      ...
     8      │  40,320    │   2,027,025      │      ...
    10      │ 3,628,800  │  17,643,225,600  │      ...
    15      │  1.3 * 10^12│  ≈ 10^17         │      ...
    20      │  2.4 * 10^18│  ≈ 10^23         │      ...

Clearly exhaustive search is impossible beyond ~10-12 tables.
```

### 4.6 Statistics Collection

```sql
-- PostgreSQL: ANALYZE command
ANALYZE users;              -- analyze one table
ANALYZE users(email);       -- analyze specific column
ANALYZE;                    -- analyze all tables in database

-- Auto-analyze: triggered when ~10% of rows change
-- Configurable: autovacuum_analyze_threshold, autovacuum_analyze_scale_factor

-- View collected statistics:
SELECT
    attname,
    n_distinct,
    most_common_vals,
    most_common_freqs,
    histogram_bounds,
    correlation
FROM pg_stats
WHERE tablename = 'users';

-- Multi-column statistics (PostgreSQL 10+):
CREATE STATISTICS users_city_state ON city, state FROM users;
ANALYZE users;

-- MySQL: ANALYZE TABLE
ANALYZE TABLE users;

-- MySQL 8.0+ histogram statistics:
ANALYZE TABLE users UPDATE HISTOGRAM ON age, status WITH 100 BUCKETS;

-- View MySQL histograms:
SELECT * FROM information_schema.column_statistics
WHERE table_name = 'users';
```

### 4.7 Plan Caching and Prepared Statements

```sql
-- PostgreSQL prepared statement lifecycle:
PREPARE user_lookup(int) AS SELECT * FROM users WHERE id = $1;
EXECUTE user_lookup(42);

-- PostgreSQL 12+ behavior:
-- First 5 executions: generate custom plan each time (using parameter values)
-- After 5 executions: if generic plan cost <= 1.1 * avg(custom plan cost),
--   switch to a cached generic plan.
-- Otherwise: continue generating custom plans.

-- Why this matters:
-- Generic plan for: WHERE status = $1
--   Uses average selectivity -- could be very wrong for skewed data.
--   status = 'active' (72% of rows) vs status = 'banned' (0.1% of rows)
--   need very different plans (seq scan vs index scan).
```

```
Plan Cache Decision Flow (PostgreSQL 12+):

  PREPARE stmt(...)
       │
       ▼
  ┌─ Execution 1-5: Custom plan (use actual parameter values) ─┐
  │    Record custom plan costs: c1, c2, c3, c4, c5            │
  └──────────────────────┬─────────────────────────────────────┘
                         │
                         ▼
              Generate generic plan (no parameter values)
              Cost = g
                         │
                         ▼
              g <= 1.1 * avg(c1..c5) ?
              ┌──── YES ────┐──── NO ────┐
              ▼             ▼            │
        Use generic     Use custom      │
        plan always     plan always     │
                        (re-evaluate    │
                         periodically)  │
```

### 4.8 Adaptive Query Execution

Traditional optimizers commit to a plan before execution begins. Adaptive execution allows the engine to change the plan mid-query when runtime information shows the original estimates were wrong.

```
Adaptive Execution Strategies:

1. ADAPTIVE JOINS (Oracle 12c+):
   Start executing as Nested Loop Join.
   If actual row count exceeds threshold, switch to Hash Join mid-stream.

   ┌──────────────┐
   │ Statistics    │       ┌───────────────┐
   │ Collector     │──────▶│ Plan Switcher │
   │ (counting     │       │ (if rows >    │
   │  actual rows) │       │  threshold,   │
   └──────────────┘       │  rebuild as   │
                           │  hash join)   │
                           └───────────────┘

2. ADAPTIVE REPARTITIONING (Spark AQE):
   After a shuffle, observe actual partition sizes.
   Coalesce small partitions, re-split large ones.
   Adjust parallelism dynamically.

3. RUNTIME FILTER PUSHDOWN:
   Build side of hash join produces a bloom filter of join keys.
   Push filter down to the probe side's scan operator.
   Filters out non-matching rows before they enter the pipeline.
   (Used by Presto/Trino, Spark, Impala)

4. CARDINALITY FEEDBACK (SQL Server CE Feedback):
   After query execution, compare estimated vs actual cardinalities.
   Store corrections. Next execution uses corrected estimates.
```

---

## 5. Join Algorithms

### 5.1 Nested Loop Join (NLJ)

The simplest join algorithm. For each row in the outer (driving) table, scan the inner table for matches.

**Simple Nested Loop:**

```
for each row r in outer_table:
    for each row s in inner_table:
        if join_condition(r, s):
            emit(r, s)

Time:  O(|outer| * |inner|)
Space: O(1)
```

**Index Nested Loop:**

```
for each row r in outer_table:
    lookup matching rows in inner_table using index on join column
    for each matching row s:
        emit(r, s)

Time:  O(|outer| * log(|inner|))  -- B-tree index
       O(|outer| * 1)             -- hash index (average case)
Space: O(1) + index
```

**Block Nested Loop:**

```
for each block B of outer_table (fits in memory buffer):
    for each row s in inner_table:
        for each row r in B:
            if join_condition(r, s):
                emit(r, s)

Time:  O(|outer| * |inner| / block_size)  -- inner scans reduced
Space: O(block_size)
```

```
Best Case for NLJ:
  ┌─────────────────────────────────────────────────────┐
  │  Small outer table (10-100 rows)                     │
  │  + Index on inner table's join column                 │
  │  = Very fast: 10-100 index lookups                    │
  │                                                       │
  │  This is why the optimizer often puts the smaller     │
  │  table on the outer side when choosing NLJ.           │
  └─────────────────────────────────────────────────────┘
```

### 5.2 Hash Join

Build a hash table on the smaller input (build side), then probe it with the larger input (probe side).

```
Phase 1 - BUILD:
  hash_table = {}
  for each row r in build_side (smaller):
      key = hash(r.join_column)
      hash_table[key].append(r)

Phase 2 - PROBE:
  for each row s in probe_side (larger):
      key = hash(s.join_column)
      for each row r in hash_table[key]:
          if join_condition(r, s):
              emit(r, s)

Time:  O(|build| + |probe|)  -- linear!
Space: O(|build|)            -- hash table must fit in memory
```

```
Hash Join in Memory:

  Build Side              Hash Table              Probe Side
  (smaller)               (in memory)             (larger)
  ┌─────┐                ┌─────────┐             ┌─────┐
  │ r1  │──hash(r1.x)──▶ │ bucket0 │◀──hash(s1.x)──│ s1  │
  │ r2  │──hash(r2.x)──▶ │ bucket1 │◀──hash(s2.x)──│ s2  │
  │ r3  │──hash(r3.x)──▶ │ bucket2 │◀──hash(s3.x)──│ s3  │
  │ ... │                │ ...     │             │ ... │
  └─────┘                └─────────┘             └─────┘
                          ▲
                          │
                    Must fit in
                    work_mem!
```

**Grace Hash Join (for data larger than memory):**

When the build side does not fit in memory, the data is partitioned to disk:

```
Phase 0 - PARTITION (both sides):
  Partition build_side into P partitions using hash function h1
  Partition probe_side into P partitions using same hash function h1
  Write partitions to disk (temporary files)

  Build Partitions:    B0  B1  B2  ...  Bp-1    (on disk)
  Probe Partitions:    P0  P1  P2  ...  Pp-1    (on disk)

Phase 1+2 - For each partition i:
  Load Bi into memory hash table (using different hash function h2)
  Stream Pi through, probing the hash table
  Emit matches

  Key: h1 guarantees matching rows are in same-numbered partitions
  So we never need to compare Bi with Pj (i != j)

If a partition still does not fit in memory: recursively partition it.
```

### 5.3 Sort-Merge Join

Sort both inputs on the join column, then merge them in a single pass.

```
Phase 1 - SORT:
  Sort left_input on join column(s)
  Sort right_input on join column(s)

Phase 2 - MERGE:
  l = first row of left_input
  r = first row of right_input
  while l and r are not exhausted:
      if l.key < r.key:
          advance l
      elif l.key > r.key:
          advance r
      else:  -- l.key == r.key
          emit all combinations of matching rows
          advance past the matching group

Time:  O(|L| log |L| + |R| log |R| + |L| + |R|)  -- sort + merge
       O(|L| + |R|) if inputs are already sorted
Space: O(sort buffer size) or O(1) if pre-sorted
```

**When sort-merge wins:**

```
1. Both inputs are already sorted (from an index or preceding ORDER BY)
   → Merge is essentially free: O(|L| + |R|)

2. The result needs to be sorted anyway (ORDER BY on join column)
   → Sort cost is "shared" with the ORDER BY

3. Many-to-many join with large groups
   → Hash join may have hash collision issues
   → Sort-merge handles this cleanly
```

### 5.4 Decision Matrix: When the Optimizer Picks Each Algorithm

```
┌──────────────────┬────────────────────┬──────────────────┬─────────────────────┐
│  Characteristic  │  Nested Loop Join  │    Hash Join     │   Sort-Merge Join   │
├──────────────────┼────────────────────┼──────────────────┼─────────────────────┤
│ Best when        │ Small outer +      │ Large inputs,    │ Inputs already      │
│                  │ indexed inner      │ equi-join        │ sorted, or need     │
│                  │                    │                  │ sorted output       │
├──────────────────┼────────────────────┼──────────────────┼─────────────────────┤
│ Join condition   │ Any (including     │ Equi-join only   │ Equi-join or        │
│                  │ non-equi, theta)   │ (needs hash)     │ inequality (>=, <=) │
├──────────────────┼────────────────────┼──────────────────┼─────────────────────┤
│ Memory needs     │ O(1)              │ O(|build side|)  │ O(sort buffers)     │
├──────────────────┼────────────────────┼──────────────────┼─────────────────────┤
│ Time complexity  │ O(N*M) or         │ O(N+M)           │ O(N log N + M log M)│
│                  │ O(N*logM) w/index  │                  │ or O(N+M) if sorted │
├──────────────────┼────────────────────┼──────────────────┼─────────────────────┤
│ Handles skew     │ Yes               │ Poorly (hot      │ Yes                 │
│                  │                    │ bucket problem)  │                     │
├──────────────────┼────────────────────┼──────────────────┼─────────────────────┤
│ Parallelizable   │ Inner side only    │ Both sides       │ Both sides          │
│                  │ (partition outer)  │ (partition both) │ (parallel sort)     │
├──────────────────┼────────────────────┼──────────────────┼─────────────────────┤
│ Supports OUTER   │ Yes (all types)   │ Yes (all types)  │ Yes (with care)     │
│ joins            │                    │                  │                     │
├──────────────────┼────────────────────┼──────────────────┼─────────────────────┤
│ Supports SEMI /  │ Yes (early        │ Yes (mark        │ Yes                 │
│ ANTI join        │ termination)      │ duplicates)      │                     │
├──────────────────┼────────────────────┼──────────────────┼─────────────────────┤
│ Startup cost     │ None (streaming)  │ High (build      │ High (sort both     │
│                  │                    │ hash table)      │ inputs first)       │
├──────────────────┼────────────────────┼──────────────────┼─────────────────────┤
│ First row latency│ Low               │ Medium           │ High                │
└──────────────────┴────────────────────┴──────────────────┴─────────────────────┘
```

### 5.5 Parallel Hash Joins

```
Parallel Hash Join (PostgreSQL 11+):

  Multiple workers cooperatively build a SHARED hash table,
  then each worker probes a partition of the probe side independently.

  Worker 1 ──┐                         ┌── Worker 1 (probe partition 1)
  Worker 2 ──┼── Build shared ────────▶├── Worker 2 (probe partition 2)
  Worker 3 ──┤   hash table            ├── Worker 3 (probe partition 3)
  Worker 4 ──┘   (barrier sync)        └── Worker 4 (probe partition 4)

  Key challenges:
  1. Concurrent hash table insertion (lock-free or partitioned)
  2. Barrier synchronization between build and probe phases
  3. Memory accounting across workers (shared work_mem)
  4. Handling skew: if one hash bucket is huge, one probe worker
     gets all the work (load imbalance)
```

### 5.6 Join Tree Shapes

```
Left-Deep:              Right-Deep:              Bushy:

       ⋈                    ⋈                      ⋈
      / \                  / \                    /   \
     ⋈   D              A   ⋈                  ⋈     ⋈
    / \                     / \                / \   / \
   ⋈   C                  B   ⋈              A   B C   D
  / \                         / \
 A   B                       C   D

Properties:
- Left-deep: pipeline-friendly. Each join's output feeds the next join.
  Outer side is always the growing intermediate result.
  Inner side is always a base table (can use index).

- Right-deep: good for hash joins. Build hash tables on all base tables,
  then probe in sequence. All hash tables must fit in memory simultaneously.

- Bushy: most flexible, smallest search space to find optimal plan,
  but exponentially more plans to consider. Allows maximum parallelism
  (independent subtrees can execute concurrently).

PostgreSQL: considers left-deep trees by default.
  With enable_bushy_joins or GEQO: considers bushy trees.

SQL Server (Cascades optimizer): considers all tree shapes.
```

---

## 6. Execution Models

### 6.1 Volcano / Iterator Model (Pull-Based, Tuple-at-a-Time)

The Volcano model, introduced by Goetz Graefe (1994), is the most widely used execution model in traditional databases. Each operator implements three methods: `Open()`, `Next()`, and `Close()`.

```
Interface:
  class Operator:
      def Open(self):   # Initialize state (open files, allocate memory)
      def Next(self):   # Return the next tuple, or NULL if exhausted
      def Close(self):  # Release resources
```

**Example execution for:**
```sql
SELECT name FROM users WHERE age > 21 ORDER BY name LIMIT 5
```

```
Call graph (pull-based: each operator pulls from its child):

  Limit(5)
    │
    │ .Next() ──────────────────────────────────────────────┐
    ▼                                                       │
  Sort(name)                                                │
    │                                                       │
    │ .Next() pulls ALL rows from child into sort buffer    │
    │ then returns sorted rows one at a time                │
    ▼                                                       │
  Filter(age > 21)                                          │
    │                                                       │
    │ .Next() → calls child.Next()                          │
    │           if row passes age > 21: return it           │
    │           otherwise: call child.Next() again          │
    ▼                                                       │
  SeqScan(users)                                            │
    │                                                       │
    │ .Next() → read next row from table pages              │
    │           return row                                  │
    │                                                       │
    └───────────────────────────────────────────────────────┘
```

**Pseudocode for Filter operator:**

```python
class Filter:
    def __init__(self, child, predicate):
        self.child = child
        self.predicate = predicate

    def Open(self):
        self.child.Open()

    def Next(self):
        while True:
            row = self.child.Next()
            if row is None:
                return None          # no more rows
            if self.predicate(row):
                return row           # passes filter
            # else: discard and try next row

    def Close(self):
        self.child.Close()
```

**Pros:**

- Simple to implement. Each operator is independent.
- Composable. Any operator can be a child of any other.
- Memory efficient. Only one tuple in flight at a time.
- Easy to add new operators.

**Cons:**

```
Performance problems with Volcano:

1. VIRTUAL FUNCTION CALL OVERHEAD
   Each .Next() call is a virtual function dispatch.
   For a query returning 1M rows through 5 operators:
     5M virtual calls (branch mispredictions, instruction cache misses)

2. POOR CACHE LOCALITY
   Each .Next() call processes ONE tuple.
   The tuple goes through operator A's code, then B's code, then C's code.
   By the time we process the next tuple in operator A,
   A's code has been evicted from L1 instruction cache.

   Tuple 1: A → B → C → D → output
   Tuple 2: A → B → C → D → output  (A's code reloaded into L1)
   Tuple 3: A → B → C → D → output  (A's code reloaded again)

3. INTERPRETATION OVERHEAD
   Expression evaluation (e.g., age > 21) is interpreted:
   each expression node is a tagged union with a virtual dispatch.
   For a simple comparison, this is 10-50x slower than compiled code.

4. PIPELINE BREAKERS
   Some operators (Sort, Hash Build) must consume ALL input before
   producing any output. These are "pipeline breakers" that force
   materialization of intermediate results.
```

### 6.2 Vectorized Execution (Batch-at-a-Time)

Instead of processing one tuple at a time, process a batch (vector) of 1024-4096 values at a time. This amortizes interpretation overhead and enables SIMD.

```
Key idea: replace Next() → Tuple with NextBatch() → ColumnBatch

  Volcano:          row = [name="Alice", age=30, status="active"]
  Vectorized:       names  = ["Alice", "Bob", "Carol", ..., "Zara"]   (1024 values)
                    ages   = [30, 25, 45, ..., 33]                    (1024 values)
                    status = ["active", "inactive", ..., "active"]    (1024 values)
```

```
Vectorized Filter execution:

Input batch (column-oriented):
  ages   = [30, 17, 45, 19, 22, 55, 20, 28, ...]   (1024 values)

Step 1: Evaluate predicate on entire batch using SIMD
  mask = ages > 21
       = [ 1,  0,  1,  0,  1,  1,  0,  1, ...]

Step 2: Compact matching rows using selection vector
  selected_positions = [0, 2, 4, 5, 7, ...]

Step 3: Pass selection vector to parent operator
  (No data copying -- just pass the positions of qualifying rows)
```

**SIMD utilization example:**

```
Scalar code (one comparison per instruction):
  for i in range(1024):
      mask[i] = ages[i] > 21

SIMD code (AVX-512: 16 int32 comparisons per instruction):
  for i in range(0, 1024, 16):      # 64 iterations instead of 1024
      mask[i:i+16] = _mm512_cmpgt_epi32(ages[i:i+16], 21)

Speedup: 10-16x for simple predicates (limited by memory bandwidth)
```

**Why batch-at-a-time is faster:**

```
┌──────────────────────────────────────────────────────────────┐
│          WHY VECTORIZED IS 5-50x FASTER                      │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│  1. AMORTIZED INTERPRETATION OVERHEAD                        │
│     Volcano: 1 virtual call per tuple per operator           │
│     Vectorized: 1 virtual call per 1024 tuples per operator  │
│     → 1024x fewer virtual calls                              │
│                                                              │
│  2. TIGHT INNER LOOPS                                        │
│     The inner loop processes a batch with a simple for loop  │
│     → Compiler can auto-vectorize (SIMD)                     │
│     → CPU branch predictor has simple pattern                │
│     → Prefetcher can stream data from memory                 │
│                                                              │
│  3. CACHE LOCALITY                                           │
│     Column batches fit in L1/L2 cache                        │
│     All 1024 values of one column processed before moving on │
│     → No cache thrashing between operators                   │
│                                                              │
│  4. SIMD (Single Instruction, Multiple Data)                 │
│     Process 4/8/16/32 values per instruction                 │
│     Particularly effective for: comparisons, arithmetic,     │
│     hash computation, string operations on fixed-width data  │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

**Systems using vectorized execution:**

| System | Batch Size | SIMD | Notes |
|--------|-----------|------|-------|
| DuckDB | 2048 | AVX2, NEON | Full vectorized engine, columnar storage |
| ClickHouse | 65,536 | AVX2, AVX-512 | Columnar OLAP, extremely fast scans |
| Velox (Meta) | 1024 | Varies | Execution engine used by Presto, Spark |
| DataFusion (Apache) | 8192 | Via Arrow | Rust-based, uses Apache Arrow format |
| Photon (Databricks) | Varies | AVX2 | C++ vectorized engine for Spark |
| PostgreSQL 16+ | (limited) | (limited) | Some vectorized decompression, not full |

### 6.3 Push-Based / Compiled Execution

Instead of pulling tuples through an operator tree, generate native code that pushes data through a pipeline. This eliminates all interpretation overhead.

**Data-Centric Code Generation (Hyper/Umbra approach):**

```
For the query:
  SELECT name FROM users WHERE age > 21 AND status = 'active'

Traditional Volcano (interpreted):
  LimitOp → FilterOp(age>21) → FilterOp(status='active') → SeqScanOp

Compiled code (conceptual):
  for each page in users_table:
      for each tuple in page:
          age = tuple.get_column(2)        // direct memory access
          if age <= 21: continue           // branch, no virtual call
          status = tuple.get_column(3)
          if status != 'active': continue
          name = tuple.get_column(1)
          output_buffer.append(name)       // write result directly
```

**Pipeline compilation:**

The key insight is that a pipeline is a chain of operators between two pipeline breakers (materializing points). Within a pipeline, data flows through registers without materialization.

```
Query Plan:
  HashJoin ─── Build: HashAgg(orders, GROUP BY user_id)
      │
      └── Probe: Filter(users, age>21)

Two Pipelines:

Pipeline 1 (Build):                  Pipeline 2 (Probe):
  Scan(orders)                         Scan(users)
    → Aggregate(GROUP BY user_id)        → Filter(age > 21)
    → Build Hash Table                   → Probe Hash Table
                                         → Output

Generated code for Pipeline 2:
┌─────────────────────────────────────────────────────────┐
│  // Compiled function -- no virtual calls, no interp.    │
│  void pipeline2(HashTable* ht, Buffer* output) {         │
│      for (Page* page : users_table.pages()) {            │
│          for (Row row : page->rows()) {                  │
│              int age = row.get<int>(COL_AGE);            │
│              if (age <= 21) continue;                    │
│              // probe hash table                         │
│              uint64_t hash = hash_fn(row.get(COL_ID));   │
│              for (Entry* e = ht->lookup(hash); e; e=..) {│
│                  if (e->key == row.get(COL_ID)) {        │
│                      output->emit(row, e->payload);      │
│                  }                                       │
│              }                                           │
│          }                                               │
│      }                                                   │
│  }                                                       │
└─────────────────────────────────────────────────────────┘
```

**JIT compilation vs Interpretation:**

| Approach | Compilation Time | Execution Speed | Used By |
|----------|-----------------|----------------|---------|
| Pure interpretation (Volcano) | 0 ms | Slowest | Most OLTP databases |
| Vectorized interpretation | 0 ms | 5-10x faster | DuckDB, ClickHouse |
| JIT compilation (LLVM) | 10-100 ms | 10-50x faster | PostgreSQL (optional), Spark (Tungsten) |
| Ahead-of-time compilation | N/A (pre-compiled) | Fastest | Hyper/Umbra (C++ codegen) |
| Adaptive (interpret first, JIT hot paths) | Variable | Best of both | Umbra, some JVMs |

**Trade-off:** JIT compilation has a startup cost. For short OLTP queries (< 1ms), the compilation time dominates. For long OLAP queries (seconds to hours), compilation time is negligible and execution speed dominates.

```
When to JIT vs Interpret:

Total Time │
           │  Interpret
           │  /
           │ /
           │/        ─── JIT (compile + execute)
           ├─────/───────────────────────
           │   /
           │  / compile
           │ /  overhead
           │/
           ├──────────────────────────── Data Size
           ↑
        Crossover point
        (~100K - 1M rows typically)

PostgreSQL JIT threshold: jit_above_cost = 100000
  (only JIT-compile queries with estimated cost > 100K)
```

### 6.4 Morsel-Driven Parallelism

Introduced by the HyPer system (Leis et al., 2014). Combines compiled execution with fine-grained parallelism.

```
Key Concepts:

MORSEL: A fixed-size chunk of input data (~10,000 rows).
         Small enough for L2/L3 cache. Large enough to amortize
         scheduling overhead.

TASK:    A pipeline applied to one morsel.
         Tasks are the unit of work-stealing.

DISPATCHER: Assigns morsels to worker threads.
            Workers steal morsels from each other when idle.
```

```
┌──────────────────────────────────────────────────────────────────┐
│                  MORSEL-DRIVEN PARALLELISM                        │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  Table Data (partitioned into morsels):                          │
│  ┌────┐┌────┐┌────┐┌────┐┌────┐┌────┐┌────┐┌────┐              │
│  │ M0 ││ M1 ││ M2 ││ M3 ││ M4 ││ M5 ││ M6 ││ M7 │              │
│  └──┬─┘└──┬─┘└──┬─┘└──┬─┘└──┬─┘└──┬─┘└──┬─┘└──┬─┘              │
│     │     │     │     │     │     │     │     │                  │
│     ▼     ▼     ▼     ▼     ▼     ▼     ▼     ▼                  │
│  ┌─────────────────────────────────────────────────┐             │
│  │              MORSEL DISPATCHER                   │             │
│  │  (assigns morsels to idle workers)               │             │
│  └────────┬──────────┬──────────┬──────────┬───────┘             │
│           │          │          │          │                      │
│           ▼          ▼          ▼          ▼                      │
│       Worker 0   Worker 1   Worker 2   Worker 3                  │
│       (Core 0)   (Core 1)   (Core 2)   (Core 3)                 │
│           │          │          │          │                      │
│           │ Execute  │ Execute  │ Execute  │ Execute              │
│           │ pipeline │ pipeline │ pipeline │ pipeline             │
│           │ on morsel│ on morsel│ on morsel│ on morsel            │
│           │          │          │          │                      │
│           ▼          ▼          ▼          ▼                      │
│       ┌──────────────────────────────────────┐                   │
│       │      SHARED HASH TABLE / OUTPUT       │                   │
│       │  (thread-safe partitioned structure)  │                   │
│       └──────────────────────────────────────┘                   │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

**NUMA-Aware Scheduling:**

```
NUMA (Non-Uniform Memory Access) topology:

  ┌──────────────────┐      ┌──────────────────┐
  │  NUMA Node 0     │      │  NUMA Node 1     │
  │  ┌─────┐┌─────┐ │      │  ┌─────┐┌─────┐ │
  │  │Core0││Core1│ │      │  │Core4││Core5│ │
  │  └─────┘└─────┘ │      │  └─────┘└─────┘ │
  │  ┌─────┐┌─────┐ │      │  ┌─────┐┌─────┐ │
  │  │Core2││Core3│ │      │  │Core6││Core7│ │
  │  └─────┘└─────┘ │      │  └─────┘└─────┘ │
  │  ┌────────────┐  │      │  ┌────────────┐  │
  │  │ Local RAM  │  │◄────▶│  │ Local RAM  │  │
  │  │ (fast)     │  │ QPI  │  │ (fast)     │  │
  │  └────────────┘  │ link │  └────────────┘  │
  └──────────────────┘      └──────────────────┘

  Local memory access:  ~80 ns
  Remote memory access: ~140 ns (1.75x slower)

  NUMA-aware scheduling:
  - Place data morsels in local memory of the NUMA node
  - Schedule workers to process morsels on the same NUMA node
  - Hash table partitions are NUMA-local
  - Work stealing prefers local morsels before stealing remote ones
```

---

## 7. Aggregation and Sorting

### 7.1 External Sort

When data exceeds available memory, external sort (also called external merge sort) is used.

```
External Sort Algorithm:

Phase 1 - CREATE SORTED RUNS:
  Read M pages of data into memory (M = work_mem / page_size)
  Sort in memory (quicksort or introsort)
  Write sorted run to temp file on disk
  Repeat until all data is consumed

  Input:    [7,2,9,1,4,8,3,6,5]  (9 values, memory fits 3)

  Run 0:    [2,7,9]    (sorted in memory, written to disk)
  Run 1:    [1,4,8]    (sorted in memory, written to disk)
  Run 2:    [3,5,6]    (sorted in memory, written to disk)

Phase 2 - MERGE RUNS:
  Open all runs simultaneously
  Use a priority queue (min-heap) to merge
  Output the smallest value from any run

  Merge:
    Heap: {2(R0), 1(R1), 3(R2)} → output 1, advance R1
    Heap: {2(R0), 4(R1), 3(R2)} → output 2, advance R0
    Heap: {7(R0), 4(R1), 3(R2)} → output 3, advance R2
    ...

  Result:   [1,2,3,4,5,6,7,8,9]
```

```
Multi-pass merge (when too many runs for available memory):

  If we have 1000 runs but can only merge M at a time:

  Pass 1: Merge runs in groups of M
           1000 runs → 1000/M merged runs

  Pass 2: Merge the merged runs
           1000/M runs → 1000/M^2 merged runs

  Continue until 1 run remains.

  Number of passes: ceil(log_M(N_runs))

  Example: 1000 runs, M=100 merge ways
    Pass 1: 1000 → 10 merged runs
    Pass 2: 10 → 1 final sorted output
    Total: 2 passes over the data
```

**PostgreSQL sort implementation:**

```
work_mem = 256MB (default 4MB -- increase for large sorts!)

Sort method selection:
  Data fits in work_mem → In-memory quicksort
  Data exceeds work_mem → External sort with polyphase merge
  Data is small (< ~1000 tuples) → Insertion sort or heapsort

External sort temp files:
  Written to: temp_tablespaces (or default tablespace)
  PostgreSQL uses replacement selection for initial run generation
  (produces runs of ~2x work_mem on average for nearly-sorted data)
```

### 7.2 Hash-Based Aggregation

Build a hash table keyed on the GROUP BY columns. Each hash entry stores the aggregate state (running sum, count, etc.).

```
Query: SELECT dept, COUNT(*), AVG(salary) FROM employees GROUP BY dept

Hash Aggregation:

  Hash Table:
  ┌──────────┬───────┬──────────┬──────────────┐
  │  dept    │ count │ sum_sal  │ sum_sal/count │
  ├──────────┼───────┼──────────┼──────────────┤
  │ "eng"    │  450  │ 67500000 │   150000     │
  │ "sales"  │  200  │ 16000000 │    80000     │
  │ "hr"     │   50  │  3500000 │    70000     │
  │ "ops"    │  100  │  8000000 │    80000     │
  └──────────┴───────┴──────────┴──────────────┘

Algorithm:
  hash_table = {}
  for each row in input:
      key = hash(row.dept)
      if key in hash_table:
          hash_table[key].count += 1
          hash_table[key].sum_sal += row.salary
      else:
          hash_table[key] = {count: 1, sum_sal: row.salary}

  for each entry in hash_table:
      emit(entry.dept, entry.count, entry.sum_sal / entry.count)

Time:  O(N) for N input rows (amortized O(1) hash table operations)
Space: O(G) where G = number of groups
```

**When hash table exceeds memory:** Spill partitions to disk (similar to Grace Hash Join). Process each partition separately.

### 7.3 Sort-Based Aggregation

Sort the input by GROUP BY columns, then scan linearly. Adjacent rows with the same key form a group.

```
Algorithm:
  sorted_input = sort(input, key=dept)

  current_group = None
  count = 0
  sum_sal = 0

  for each row in sorted_input:
      if row.dept != current_group:
          if current_group is not None:
              emit(current_group, count, sum_sal / count)
          current_group = row.dept
          count = 0
          sum_sal = 0
      count += 1
      sum_sal += row.salary

  emit(current_group, count, sum_sal / count)  # last group

Time:  O(N log N) for sort + O(N) for scan = O(N log N)
Space: O(sort buffer)  -- streaming scan needs O(1) beyond sort
```

**When to prefer sort-based over hash-based:**

| Factor | Hash Agg | Sort Agg |
|--------|----------|----------|
| Input already sorted | Wastes the ordering | Free (streaming) |
| Few groups (G << N) | Efficient | Wasteful (sort all N rows) |
| Many groups (G ~ N) | Large hash table, may spill | Sort is predictable |
| Output needs ORDER BY on GROUP BY cols | Needs separate sort | Already sorted |
| GROUP BY with DISTINCT aggregates | Multiple hash tables | Sort once, compute all |
| Parallel execution | Easy to partition | Parallel sort is harder |

### 7.4 Top-N Optimization (Heap Sort)

For queries with `ORDER BY ... LIMIT N`, there is no need to sort the entire result. A min-heap (or max-heap) of size N suffices.

```sql
SELECT * FROM products ORDER BY price DESC LIMIT 10;
```

```
Top-N with Heap:

  Maintain a MIN-HEAP of size 10 (for top 10 largest).

  For each row:
    if heap.size < 10:
        heap.insert(row)
    elif row.price > heap.min():
        heap.replace_min(row)
    // else: skip (row is smaller than smallest in top-10)

  Time:  O(N * log(K))  where K=10, N=total rows
  Space: O(K)           -- only 10 rows in memory

  vs full sort: O(N * log(N)) time, O(N) space
  For N=1,000,000 and K=10: ~20x faster
```

PostgreSQL recognizes this pattern and uses an "Incremental Sort" or "Top-N Heapsort" plan node.

### 7.5 Spilling to Disk

When memory is insufficient for hash tables or sort buffers, the engine must spill data to temporary files on disk.

```
Hash Aggregation Spill Strategy:

1. Hash input rows into P partitions using hash function h1
2. Process partitions one at a time:
   - If partition fits in memory: aggregate in-memory
   - If partition is too large: recursively repartition with h2

Spill Trigger:
  PostgreSQL: when hash table exceeds work_mem
  hash_mem_multiplier (PG 13+): allows hash aggs to use
    work_mem * hash_mem_multiplier before spilling

Sort Spill Strategy:
  Create sorted runs on disk (each run = work_mem sized chunk)
  Merge runs in multiple passes if needed

Monitoring spill in PostgreSQL:
  EXPLAIN (ANALYZE, BUFFERS) shows:
    Sort Method: external merge  Disk: 45672kB
    -- This means the sort spilled 45MB to disk
    -- Increase work_mem to avoid this:
    SET work_mem = '256MB';
```

---

## 8. Parallel and Distributed Query Execution

### 8.1 Intra-Operator Parallelism

Partition a single operator's work across multiple threads/processes.

```
Parallel Sequential Scan:

  Table: users (1,000,000 rows, 10,000 pages)

  Leader process assigns page ranges to workers:

  Worker 0: pages [0, 2500)
  Worker 1: pages [2500, 5000)
  Worker 2: pages [5000, 7500)
  Worker 3: pages [7500, 10000)

  Each worker scans its range and applies filters independently.
  Results are gathered by the leader.

  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐
  │Worker 0 │  │Worker 1 │  │Worker 2 │  │Worker 3 │
  │pg 0-2499│  │pg 2500- │  │pg 5000- │  │pg 7500- │
  │         │  │  4999   │  │  7499   │  │  9999   │
  └────┬────┘  └────┬────┘  └────┬────┘  └────┬────┘
       │            │            │            │
       └──────┬─────┴──────┬─────┘            │
              │            │                  │
              ▼            ▼                  ▼
         ┌──────────────────────────────────────┐
         │              GATHER                   │
         │  (Collect results from all workers)   │
         └──────────────────────────────────────┘
```

**Parallel Hash Join:**

```
Build Phase (parallel):
  All workers read the build side in parallel.
  Each worker inserts into a SHARED hash table
  (using lock-free or partitioned insertion).

  ┌────────┐ ┌────────┐ ┌────────┐
  │Worker 0│ │Worker 1│ │Worker 2│    Reading build side
  └───┬────┘ └───┬────┘ └───┬────┘    in parallel
      │          │          │
      ▼          ▼          ▼
  ┌────────────────────────────────┐
  │     SHARED HASH TABLE          │   All workers contribute
  │  (lock-free / partitioned)     │
  └────────────────────────────────┘

  ── BARRIER (wait for all workers to finish building) ──

Probe Phase (parallel):
  All workers read the probe side in parallel.
  Each worker probes the shared hash table independently.

  ┌────────┐ ┌────────┐ ┌────────┐
  │Worker 0│ │Worker 1│ │Worker 2│    Reading probe side
  └───┬────┘ └───┬────┘ └───┬────┘    in parallel
      │          │          │
      │ probe    │ probe    │ probe
      ▼          ▼          ▼
  ┌────────────────────────────────┐
  │     SHARED HASH TABLE          │   Read-only (concurrent)
  └────────────────────────────────┘
```

### 8.2 Inter-Operator Parallelism

Execute different operators concurrently in a pipeline.

```
Pipeline parallelism:

  Time ──────────────────────────────────────▶

  Operator A (Scan):     [████████████]
                                ↓ rows flow
  Operator B (Filter):     [████████████]
                                ↓ rows flow
  Operator C (Join):         [████████████]

  All three operators execute simultaneously.
  A produces rows that B consumes, B produces rows that C consumes.
  This is natural in the Volcano model (pull-based pipelining).

  Limitation: Pipeline breaks at materializing operators
  (Sort, Hash Build, Aggregate). These must consume ALL input
  before producing ANY output.
```

### 8.3 Exchange Operators

Exchange operators are the mechanism for parallelism and distribution. They repartition, broadcast, or gather data between operator instances.

```
┌──────────────────────────────────────────────────────────────────┐
│                    EXCHANGE OPERATORS                             │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  1. GATHER (N:1)                                                 │
│     Collect results from N parallel workers into one stream.     │
│     ┌──┐ ┌──┐ ┌──┐                                              │
│     │W1│ │W2│ │W3│                                               │
│     └─┬┘ └─┬┘ └─┬┘                                              │
│       └──┬──┘   │                                                │
│          └──┬───┘                                                │
│             ▼                                                    │
│         ┌──────┐                                                 │
│         │GATHER│                                                 │
│         └──────┘                                                 │
│                                                                  │
│  2. GATHER MERGE (N:1, preserving sort order)                    │
│     Like GATHER but merge-sorts N sorted streams.                │
│     Output is sorted without a separate Sort operator.           │
│                                                                  │
│  3. REPARTITION / REDISTRIBUTE (N:M)                             │
│     Hash-partition data by a key across M workers.               │
│     Used before hash joins and hash aggregations to ensure       │
│     matching keys land on the same worker.                       │
│     ┌──┐ ┌──┐ ┌──┐                                              │
│     │W1│ │W2│ │W3│  (each sends rows to destination by hash)     │
│     └─┬┘ └─┬┘ └─┬┘                                              │
│       │╲ ╱│╲ ╱│                                                  │
│       │ ╳ │ ╳ │     (all-to-all shuffle)                         │
│       │╱ ╲│╱ ╲│                                                  │
│     ┌─┴┐ ┌┴─┐ ┌┴─┐                                              │
│     │W4│ │W5│ │W6│                                               │
│     └──┘ └──┘ └──┘                                               │
│                                                                  │
│  4. BROADCAST (1:N)                                              │
│     Send a copy of all data to every worker.                     │
│     Used when one side of a join is small (< broadcast_threshold)│
│     ┌──────┐                                                     │
│     │Source│                                                     │
│     └──┬───┘                                                     │
│        │ (copy to all)                                           │
│     ┌──┼──┐                                                      │
│     ▼  ▼  ▼                                                      │
│    ┌──┐┌──┐┌──┐                                                  │
│    │W1││W2││W3│ (each has full copy)                             │
│    └──┘└──┘└──┘                                                  │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

### 8.4 Distributed Query Planning

In distributed databases (CockroachDB, Spanner, Citus, TiDB, YugabyteDB), the query planner must account for data location and network costs.

```
Two-Phase Optimization:

Phase 1: Logical + Physical optimization (ignore distribution)
  Produce a single-node optimal plan as if all data were local.

Phase 2: Distribution-aware optimization
  Determine data placement for each table.
  Insert exchange operators (shuffle, broadcast, gather).
  Choose between:
    - Broadcast join: send small table to all nodes
    - Shuffle join:   repartition both tables by join key
    - Colocated join: if both tables are partitioned by join key
                      (no data movement needed!)

  Cost model additions:
    network_cost = data_size * network_latency_per_byte
    shuffle_cost = both_sides_data * 2 * network_cost
    broadcast_cost = small_side * num_nodes * network_cost
```

```
Distributed Join Decision:

  Table A: 100 MB, partitioned by A.id
  Table B: 10 GB, partitioned by B.id
  Join:    A.id = B.id

  Option 1: Shuffle both (repartition by join key)
    Cost: (100MB + 10GB) * network = 10.1 GB transferred
    BUT: A and B are ALREADY partitioned by id!
    → This is a COLOCATED JOIN: 0 bytes transferred.

  Table A: 100 MB, partitioned by A.id
  Table C: 10 GB, partitioned by C.date
  Join:    A.id = C.user_id

  Option 1: Shuffle both by user_id
    Cost: (100MB + 10GB) * network = 10.1 GB transferred

  Option 2: Broadcast A to all nodes
    Cost: 100MB * N_nodes * network
    If N_nodes = 10: 1 GB transferred (10x less!)

  → Broadcast wins when one side is much smaller than the other.
```

### 8.5 Shuffle Operations and Data Movement Cost

```
Shuffle (Repartition) Deep Dive:

  Source partition:                  Target partition:
  (by customer_id)                  (by order_date)

  Node 1: Customers 1-1000         Node 1: Orders Jan-Mar
  Node 2: Customers 1001-2000      Node 2: Orders Apr-Jun
  Node 3: Customers 2001-3000      Node 3: Orders Jul-Sep

  Every source node must send data to every target node.
  This is an all-to-all communication pattern.

  Network traffic = Total_Data_Size
  (each byte is sent exactly once, but to a different destination)

  Performance bottleneck: network bisection bandwidth
  If each node has 10 Gbps NIC and there are 100 nodes:
    Max shuffle throughput = 10 Gbps per node
    For 1 TB shuffle: 1TB / 10Gbps = ~800 seconds
    With compression (3x): ~270 seconds
    With overlap (pipeline): less wall-clock time

Optimizations:
  1. Predicate pushdown BEFORE shuffle (reduce data to move)
  2. Partial aggregation BEFORE shuffle (combine locally first)
  3. Bloom filter pushdown (skip rows that won't join)
  4. Colocated joins (avoid shuffle entirely)
  5. Adaptive repartitioning (Spark AQE: coalesce small partitions)
```

---

## 9. Query Explain Plans

### 9.1 Reading PostgreSQL EXPLAIN Output

```sql
EXPLAIN SELECT u.name, COUNT(o.id) AS order_count
FROM users u
JOIN orders o ON u.id = o.user_id
WHERE u.status = 'active'
GROUP BY u.name
ORDER BY order_count DESC
LIMIT 10;
```

```
                                         QUERY PLAN
──────────────────────────────────────────────────────────────────────────────
 Limit  (cost=15234.56..15234.58 rows=10 width=40)
   ->  Sort  (cost=15234.56..15290.12 rows=22222 width=40)
         Sort Key: (count(o.id)) DESC
         ->  HashAggregate  (cost=14678.90..14901.12 rows=22222 width=40)
               Group Key: u.name
               ->  Hash Join  (cost=3456.00..14123.45 rows=111111 width=36)
                     Hash Cond: (o.user_id = u.id)
                     ->  Seq Scan on orders o  (cost=0.00..8234.00 rows=500000 width=8)
                     ->  Hash  (cost=2345.00..2345.00 rows=72000 width=36)
                           ->  Seq Scan on users u  (cost=0.00..2345.00 rows=72000 width=36)
                                 Filter: (status = 'active'::text)
```

**How to read cost numbers:**

```
cost=STARTUP..TOTAL

  STARTUP cost:  Cost before the first row can be returned.
                 For Seq Scan: 0 (can return first row immediately)
                 For Sort: high (must read all rows before returning any)
                 For Hash Join: cost of building the hash table

  TOTAL cost:    Cost to return ALL rows.

  rows:          Estimated number of output rows.
  width:         Estimated average width of output rows in bytes.

  Cost units are arbitrary but proportional.
  Roughly: cost of 1.0 ≈ reading one sequential disk page.

Reading the plan bottom-up:
  1. Seq Scan on users: read all users, filter status='active' → 72,000 rows
  2. Hash: build hash table on those 72,000 rows
  3. Seq Scan on orders: read all 500,000 orders
  4. Hash Join: probe hash table → 111,111 matching rows
  5. HashAggregate: group by name → 22,222 groups
  6. Sort: sort by count desc → 22,222 rows
  7. Limit: return top 10
```

### 9.2 Reading EXPLAIN ANALYZE

`EXPLAIN ANALYZE` actually executes the query and reports real times and row counts.

```sql
EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) SELECT ...
```

```
                                            QUERY PLAN
─────────────────────────────────────────────────────────────────────────────────
 Limit  (cost=15234.56..15234.58 rows=10 width=40)
        (actual time=245.123..245.130 rows=10 loops=1)
   ->  Sort  (cost=15234.56..15290.12 rows=22222 width=40)
              (actual time=245.120..245.125 rows=10 loops=1)
         Sort Key: (count(o.id)) DESC
         Sort Method: top-N heapsort  Memory: 26kB
         ->  HashAggregate  (cost=14678.90..14901.12 rows=22222 width=40)
                            (actual time=230.456..238.789 rows=18543 loops=1)
               Group Key: u.name
               Batches: 1  Memory Usage: 3521kB
               ->  Hash Join  (cost=3456.00..14123.45 rows=111111 width=36)
                              (actual time=45.678..198.234 rows=287456 loops=1)
                     Hash Cond: (o.user_id = u.id)
                     ->  Seq Scan on orders o  (cost=0.00..8234.00 rows=500000 width=8)
                                              (actual time=0.012..78.345 rows=500000 loops=1)
                           Buffers: shared hit=4234 read=4000
                     ->  Hash  (cost=2345.00..2345.00 rows=72000 width=36)
                                (actual time=45.123..45.123 rows=72345 loops=1)
                           Buckets: 131072  Batches: 1  Memory Usage: 5234kB
                           ->  Seq Scan on users u  (cost=0.00..2345.00 rows=72000 width=36)
                                                    (actual time=0.008..23.456 rows=72345 loops=1)
                                 Filter: (status = 'active'::text)
                                 Rows Removed by Filter: 27655
                                 Buffers: shared hit=1345
 Planning Time: 0.654 ms
 Execution Time: 245.567 ms
```

**Key things to look for:**

```
┌─────────────────────────────────────────────────────────────────┐
│              EXPLAIN ANALYZE CHEAT SHEET                        │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  1. ESTIMATED vs ACTUAL ROW COUNTS                              │
│     rows=111111 (estimated)  vs  rows=287456 (actual)           │
│     2.6x estimation error! This may cause suboptimal plan.     │
│     Run ANALYZE to update statistics.                           │
│                                                                 │
│  2. ACTUAL TIME                                                 │
│     actual time=START..END                                      │
│     Time is in milliseconds, CUMULATIVE (includes children).    │
│     To get self-time: subtract children's time.                 │
│                                                                 │
│     Hash Join self time:                                        │
│       198.234 (total) - 78.345 (orders scan) - 45.123 (hash    │
│       build) = 74.766 ms                                        │
│                                                                 │
│  3. LOOPS                                                       │
│     loops=1: executed once                                      │
│     loops=100: executed 100 times (e.g., inner side of NLJ)     │
│     Multiply time * loops for true total time.                  │
│                                                                 │
│  4. BUFFERS                                                     │
│     shared hit=4234: pages found in buffer cache (fast)         │
│     shared read=4000: pages read from disk (slow)               │
│     shared written=50: dirty pages written (background)         │
│     temp read/written: spill to temp files (very slow)          │
│                                                                 │
│  5. SORT METHOD                                                 │
│     "quicksort  Memory: 25kB"    → in-memory sort (good)       │
│     "external merge  Disk: 45MB" → spilled to disk (bad,       │
│                                     increase work_mem)          │
│     "top-N heapsort  Memory: 26kB" → LIMIT optimization (good) │
│                                                                 │
│  6. HASH BATCHES                                                │
│     Batches: 1    → hash table fit in memory (good)             │
│     Batches: 8    → hash table spilled to 8 batches (bad)       │
│                     increase work_mem                            │
│                                                                 │
│  7. ROWS REMOVED BY FILTER                                      │
│     Rows Removed by Filter: 27655                               │
│     These rows were read but didn't match the predicate.        │
│     If this number is very large relative to output rows,       │
│     consider adding an index on the filter column.              │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 9.3 Common Plan Nodes and What They Mean

```
┌──────────────────────┬──────────────────────────────────────────────────────────┐
│  Plan Node           │  What It Does                                            │
├──────────────────────┼──────────────────────────────────────────────────────────┤
│ Seq Scan             │ Full table scan, reading every page. Cheapest startup    │
│                      │ cost but reads everything. Fast for small tables or      │
│                      │ low-selectivity filters.                                 │
├──────────────────────┼──────────────────────────────────────────────────────────┤
│ Index Scan           │ Traverse B-tree index, then fetch heap tuple for each   │
│                      │ match. Good for high selectivity (< 5-15% of rows).     │
│                      │ Random I/O on the heap for each index entry.            │
├──────────────────────┼──────────────────────────────────────────────────────────┤
│ Index Only Scan      │ Like Index Scan but all needed columns are in the index │
│                      │ (covering index). No heap fetch needed. Fastest for     │
│                      │ queries that only need indexed columns. Requires        │
│                      │ visibility map to confirm tuples are all-visible.       │
├──────────────────────┼──────────────────────────────────────────────────────────┤
│ Bitmap Index Scan    │ Scan index, build a bitmap of matching page numbers.    │
│ + Bitmap Heap Scan   │ Then fetch pages in physical order (sequential I/O).    │
│                      │ Can combine multiple indexes with BitmapAnd/BitmapOr.   │
│                      │ Good for medium selectivity (5-25% of rows).            │
├──────────────────────┼──────────────────────────────────────────────────────────┤
│ Nested Loop          │ For each row in outer, scan inner. Watch for high       │
│                      │ loops count -- if inner has no index, this is O(N*M).   │
├──────────────────────┼──────────────────────────────────────────────────────────┤
│ Hash Join            │ Build hash table on inner, probe with outer. Fast for   │
│                      │ large equi-joins. Watch for Batches > 1 (spill).        │
├──────────────────────┼──────────────────────────────────────────────────────────┤
│ Merge Join           │ Merge two sorted inputs. Watch for child Sort nodes     │
│                      │ (sorting cost may dominate).                            │
├──────────────────────┼──────────────────────────────────────────────────────────┤
│ Sort                 │ Sort input by specified keys. Watch for external merge  │
│                      │ (disk spill). Consider adding an index or increasing    │
│                      │ work_mem.                                               │
├──────────────────────┼──────────────────────────────────────────────────────────┤
│ HashAggregate        │ Hash-based GROUP BY. Watch for Batches > 1 (spill).     │
│                      │ Fast for moderate number of groups.                     │
├──────────────────────┼──────────────────────────────────────────────────────────┤
│ GroupAggregate       │ Sort-based GROUP BY. Input must be sorted on group keys.│
│                      │ Streaming -- low memory usage. Good if pre-sorted.      │
├──────────────────────┼──────────────────────────────────────────────────────────┤
│ Materialize          │ Stores child output in memory/disk for re-scanning.     │
│                      │ Appears when inner side of NLJ needs to be re-read.    │
├──────────────────────┼──────────────────────────────────────────────────────────┤
│ Gather               │ Collects results from parallel workers. Indicates       │
│                      │ parallel query execution. Workers count shown.          │
├──────────────────────┼──────────────────────────────────────────────────────────┤
│ Gather Merge         │ Like Gather but preserves sort order from workers.      │
│                      │ Merge-sorts N sorted streams.                           │
├──────────────────────┼──────────────────────────────────────────────────────────┤
│ Append               │ Concatenates results from multiple children. Appears    │
│                      │ for UNION ALL, partitioned table scans, or inheritance. │
├──────────────────────┼──────────────────────────────────────────────────────────┤
│ Subquery Scan        │ Wraps a subquery as a scan source. Often an indicator   │
│                      │ that the optimizer could not flatten the subquery.      │
├──────────────────────┼──────────────────────────────────────────────────────────┤
│ CTE Scan             │ Scans a materialized CTE (WITH clause). In PG 12+,     │
│                      │ non-recursive CTEs may be inlined (not materialized).   │
├──────────────────────┼──────────────────────────────────────────────────────────┤
│ Memoize (PG 14+)    │ Caches results of parameterized inner scans in NLJ.    │
│                      │ Avoids redundant index lookups for repeated join keys.  │
├──────────────────────┼──────────────────────────────────────────────────────────┤
│ Incremental Sort     │ Sorts by (a, b) when input is already sorted by (a).   │
│ (PG 13+)            │ Only re-sorts within each group of (a). Much cheaper    │
│                      │ than full sort.                                         │
└──────────────────────┴──────────────────────────────────────────────────────────┘
```

### 9.4 Identifying Slow Operations

**Red flags in EXPLAIN ANALYZE output:**

```
1. LARGE ROW ESTIMATION ERRORS

   rows=100 (estimated) vs rows=500000 (actual)
   ─────────────────────────────────────────────
   Fix: Run ANALYZE on the table.
   Fix: Create multi-column statistics for correlated columns.
   Fix: Increase default_statistics_target for skewed columns.

2. SEQ SCAN ON LARGE TABLE WITH FEW RESULT ROWS

   ->  Seq Scan on users  (actual rows=5 loops=1)
         Filter: (email = 'alice@example.com')
         Rows Removed by Filter: 999995
   ─────────────────────────────────────────────
   Fix: CREATE INDEX ON users(email);

3. NESTED LOOP WITH HIGH LOOPS AND NO INDEX

   ->  Nested Loop  (actual time=0.1..45000.0 rows=1000 loops=1)
         ->  Seq Scan on orders  (actual rows=100000 loops=1)
         ->  Seq Scan on order_items  (actual rows=5 loops=100000)
               Filter: (order_id = orders.id)
               Rows Removed by Filter: 499995
   ─────────────────────────────────────────────
   100,000 loops * 500,000 rows scanned = 50 BILLION row comparisons!
   Fix: CREATE INDEX ON order_items(order_id);
   Or:  Optimizer should have chosen Hash Join (check enable_hashjoin).

4. SORT WITH DISK SPILL

   ->  Sort  (actual time=1200..1850 rows=5000000)
         Sort Key: created_at
         Sort Method: external merge  Disk: 256000kB
   ─────────────────────────────────────────────
   Fix: SET work_mem = '512MB';  (session-level for this query)
   Fix: Add index on created_at if used with LIMIT.

5. HASH JOIN WITH MULTIPLE BATCHES

   ->  Hash Join  (actual time=890..2340 rows=1000000)
         ->  Hash  (actual time=567..567 rows=2000000)
               Buckets: 65536  Batches: 16  Memory: 32768kB
   ─────────────────────────────────────────────
   16 batches = hash table spilled to disk 16 times.
   Fix: Increase work_mem or hash_mem_multiplier.

6. GATHER WITH FEW PARALLEL WORKERS

   ->  Gather  (actual time=0.5..1234.5 rows=100000)
         Workers Planned: 4  Workers Launched: 1
   ─────────────────────────────────────────────
   Only 1 of 4 planned workers launched.
   Fix: Increase max_parallel_workers_per_gather.
   Fix: Check max_parallel_workers system-wide limit.
   Fix: Check if table has parallel_workers storage parameter.

7. INDEX SCAN WITH LOW CORRELATION

   ->  Index Scan using idx_orders_date on orders
       (actual time=0.05..8900.0 rows=200000)
         Buffers: shared hit=5000 read=195000
   ─────────────────────────────────────────────
   195,000 disk reads for 200,000 rows means almost every row
   required a random disk read. Table is not clustered by this index.
   Fix: CLUSTER orders USING idx_orders_date;  (one-time, not maintained)
   Fix: Consider Bitmap Index Scan (converts random I/O to sequential).
```

### 9.5 Practical EXPLAIN Workflow

```
Step-by-step debugging workflow:

1. Start with EXPLAIN (no ANALYZE) to see the plan without executing:
   EXPLAIN SELECT ...;

2. Check for obvious problems:
   - Seq Scans where Index Scans are expected
   - Wrong join algorithm (NLJ where Hash Join expected)
   - Missing parallelism

3. Run with ANALYZE and BUFFERS for actual performance data:
   EXPLAIN (ANALYZE, BUFFERS) SELECT ...;

4. Compare estimated vs actual row counts at each node.
   Large discrepancies → stale stats → run ANALYZE.

5. Identify the node with the most self-time:
   Self-time = node's actual time - sum of children's actual times

6. For that node, determine the fix:
   - Missing index → CREATE INDEX
   - Disk spill → increase work_mem
   - Bad join algo → check statistics, consider hints/settings
   - Low parallelism → adjust parallel settings

7. For complex queries, use:
   EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT ...;
   Then visualize with:
   - https://explain.dalibo.com/
   - https://explain.depesz.com/
   - pgMustard

8. After fixing, re-run EXPLAIN (ANALYZE) to verify improvement.
```

**Quick reference for PostgreSQL tuning knobs:**

```sql
-- Per-session settings for specific queries:
SET work_mem = '256MB';                    -- sort/hash memory
SET hash_mem_multiplier = 2.0;             -- extra memory for hash ops
SET effective_cache_size = '16GB';         -- hint about OS cache
SET random_page_cost = 1.1;               -- for SSDs (default 4.0)
SET seq_page_cost = 1.0;                  -- usually leave at 1.0
SET max_parallel_workers_per_gather = 4;  -- parallel workers per query
SET jit = on;                             -- enable JIT compilation
SET jit_above_cost = 100000;              -- JIT cost threshold

-- Force specific plan choices (debugging only, not production!):
SET enable_seqscan = off;          -- force index usage
SET enable_hashjoin = off;         -- force NLJ or merge join
SET enable_nestloop = off;         -- force hash or merge join
SET enable_mergejoin = off;        -- force hash or nested loop
SET enable_material = off;         -- prevent materialization
```

---

## Summary: The Full Picture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                                                                         │
│  SQL Text ──▶ Lex ──▶ Parse ──▶ Analyze ──▶ Rewrite ──▶ Optimize       │
│                                                             │           │
│                                    ┌────────────────────────┘           │
│                                    │                                    │
│                                    ▼                                    │
│                            Physical Plan                                │
│                                    │                                    │
│                     ┌──────────────┼──────────────┐                     │
│                     ▼              ▼              ▼                     │
│                  Volcano      Vectorized     Compiled                   │
│                  (tuple)       (batch)       (native)                   │
│                     │              │              │                     │
│                     ▼              ▼              ▼                     │
│                          Result Rows/Batches                            │
│                                    │                                    │
│                                    ▼                                    │
│                              Client/App                                 │
│                                                                         │
│  Key trade-offs at each stage:                                          │
│  ┌────────────┬────────────────────────────────────────────────┐        │
│  │ Parsing    │ Speed vs error quality. Handwritten vs gen.   │        │
│  │ Analysis   │ Strictness vs compatibility. Type coercion.   │        │
│  │ Rewriting  │ Rule count vs compile time. Always beneficial.│        │
│  │ Optimizer  │ Plan quality vs optimization time. DP vs heur.│        │
│  │ Execution  │ Startup time vs throughput. Interp vs compile.│        │
│  │ Parallelism│ Overhead vs speedup. NUMA awareness.          │        │
│  └────────────┴────────────────────────────────────────────────┘        │
│                                                                         │
│  The #1 source of bad plans: wrong cardinality estimates.               │
│  The #1 fix: keep statistics current (ANALYZE) and add                  │
│  multi-column statistics for correlated columns.                        │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```
