# Data Storage Formats and Encoding: A Deep Dive

How databases serialize rows, columns, and types into bytes within pages. This document covers the physical encoding layer -- the bridge between page-level storage (covered in 01) and how data is accessed via scans (covered in 03). Understanding encoding is critical because it determines storage overhead, compression ratios, cache efficiency, and ultimately query speed.

---

## Table of Contents

1. [Row-Oriented vs Column-Oriented Physical Layout](#1-row-oriented-vs-column-oriented-physical-layout)
2. [Type Encoding and Representation](#2-type-encoding-and-representation)
3. [Fixed-Length vs Variable-Length Data](#3-fixed-length-vs-variable-length-data)
4. [NULL Representation](#4-null-representation)
5. [TOAST and Overflow Pages](#5-toast-and-overflow-pages)
6. [Data Compression](#6-data-compression)
7. [Row Versioning Formats (MVCC Storage)](#7-row-versioning-formats-mvcc-storage)
8. [Encoding Schemes in Columnar / Analytical Engines](#8-encoding-schemes-in-columnar--analytical-engines)

---

## 1. Row-Oriented vs Column-Oriented Physical Layout

### N-ary Storage Model (NSM) -- Row Stores

In row-oriented databases, all columns of a single row are stored contiguously within a page. This is the **N-ary Storage Model (NSM)**, used by PostgreSQL, MySQL/InnoDB, SQL Server (row mode), Oracle, and SQLite.

```
NSM (Row Store) -- Page Layout:

  Page
  ┌──────────────────────────────────────────────────────────────┐
  │ Header │ Slot Array                                          │
  ├────────┴─────────────────────────────────────────────────────┤
  │                                                                │
  │  Row 1: [id=1] [name="Alice"]   [age=30] [email="a@ex.com"]  │
  │  Row 2: [id=2] [name="Bob"]     [age=25] [email="b@ex.com"]  │
  │  Row 3: [id=3] [name="Charlie"] [age=35] [email="c@ex.com"]  │
  │  Row 4: [id=4] [name="Dave"]    [age=28] [email="d@ex.com"]  │
  │  ...                                                           │
  └────────────────────────────────────────────────────────────────┘

  Byte layout on disk (conceptual):
  ┌────┬───────┬────┬───────────┬────┬───────┬────┬───────────┬──...
  │ 1  │ Alice │ 30 │ a@ex.com  │ 2  │ Bob   │ 25 │ b@ex.com  │
  └────┴───────┴────┴───────────┴────┴───────┴────┴───────────┴──...
  ◄──────── Row 1 ────────────►◄──────── Row 2 ────────────────►

  Reading Row 2: one sequential read fetches ALL columns.
  Reading only "age" column for all rows: must read every byte
  on the page and skip over unwanted columns.
```

**Strengths**: Excellent for OLTP -- point lookups, single-row inserts, updates that touch all or most columns. One I/O retrieves the complete row.

**Weaknesses**: Wasteful for analytical queries that only need 2 of 50 columns. Every column is pulled into cache even if not needed.

### Decomposition Storage Model (DSM) -- Column Stores

In column-oriented databases, all values for a single column are stored contiguously. This is the **Decomposition Storage Model (DSM)**, used by DuckDB, ClickHouse, Redshift, BigQuery, Vertica, and Parquet/ORC file formats.

```
DSM (Column Store) -- Page Layout:

  Page for "id" column:
  ┌─────────────────────────────────────────┐
  │ [1] [2] [3] [4] [5] [6] ... [N]        │
  └─────────────────────────────────────────┘

  Page for "name" column:
  ┌─────────────────────────────────────────┐
  │ [Alice] [Bob] [Charlie] [Dave] ...      │
  └─────────────────────────────────────────┘

  Page for "age" column:
  ┌─────────────────────────────────────────┐
  │ [30] [25] [35] [28] [22] [31] ... [N]   │
  └─────────────────────────────────────────┘

  Page for "email" column:
  ┌─────────────────────────────────────────┐
  │ [a@ex.com] [b@ex.com] [c@ex.com] ...   │
  └─────────────────────────────────────────┘

  SELECT AVG(age) FROM users;
  → Only reads the "age" pages. Other columns never touch disk/cache.
```

**Strengths**: Analytical queries read only needed columns. Same-type values compress extremely well (see section 6). Vectorized execution operates on arrays of one type.

**Weaknesses**: Point lookups and single-row operations must gather columns from multiple locations. Inserts must write to N separate column files.

### PAX (Partition Attributes Across) -- Hybrid

PAX keeps the row-grouping of NSM at the page level but reorganizes data into column mini-groups within each page. Used by SQL Server columnstore indexes and as inspiration for Parquet's row groups.

```
PAX -- Page Layout:

  Page (contains all rows for this page, but reorganized by column)
  ┌──────────────────────────────────────────────────────────────┐
  │ Header │ Column-group offsets                                 │
  ├────────┴─────────────────────────────────────────────────────┤
  │                                                                │
  │  id mini-page:    [1] [2] [3] [4]                             │
  │  name mini-page:  [Alice] [Bob] [Charlie] [Dave]              │
  │  age mini-page:   [30] [25] [35] [28]                         │
  │  email mini-page: [a@ex.com] [b@ex.com] [c@ex.com] [d@..]   │
  │                                                                │
  └────────────────────────────────────────────────────────────────┘

  Benefits:
  - Column scans within a page are sequential (cache-friendly)
  - Reconstructing a full row stays within one page (no cross-page I/O)
  - Compression can be applied per mini-page
```

### Comparison Matrix

| Property | NSM (Row) | DSM (Column) | PAX (Hybrid) |
|----------|-----------|-------------|--------------|
| Point lookup (1 row, all cols) | Excellent (1 page read) | Poor (N column reads) | Good (1 page, column mini-scan) |
| Analytical scan (2 of 50 cols) | Poor (read all 50 cols) | Excellent (read 2 cols) | Good (skip 48 mini-pages) |
| INSERT single row | Fast (append to 1 page) | Slow (write to N columns) | Moderate |
| Compression ratio | Low (mixed types per page) | High (same type per page) | Moderate |
| Cache utilization (OLTP) | High | Low | Moderate |
| Cache utilization (OLAP) | Low | High | Moderate-High |
| Used by | PG, InnoDB, Oracle | ClickHouse, DuckDB, Redshift | SQL Server columnstore, Parquet |

---

## 2. Type Encoding and Representation

### Integer Encoding

Databases store integers in fixed-width binary format. The width depends on the declared type.

```
Type          Bytes   Range                          PostgreSQL     MySQL
──────────    ─────   ─────────────────────────────  ───────────    ──────
SMALLINT      2       -32,768 to 32,767              int2           SMALLINT
INTEGER       4       -2.1B to 2.1B                  int4           INT
BIGINT        8       -9.2×10^18 to 9.2×10^18       int8           BIGINT

Byte layout (little-endian, value = 42):

  SMALLINT (2 bytes):  [2A 00]
  INTEGER  (4 bytes):  [2A 00 00 00]
  BIGINT   (8 bytes):  [2A 00 00 00 00 00 00 00]

PostgreSQL stores integers in the platform's native byte order
(typically little-endian on x86). Index pages store them in
big-endian for correct sort order in byte-wise comparison.

InnoDB stores integers in BIG-ENDIAN in index pages (so memcmp
gives correct ordering) but in little-endian in row data.
```

### Floating Point (IEEE 754)

```
FLOAT / REAL:     4 bytes, IEEE 754 single precision
                  ~7 decimal digits of precision
                  Range: ±3.4 × 10^38

DOUBLE PRECISION: 8 bytes, IEEE 754 double precision
                  ~15 decimal digits of precision
                  Range: ±1.7 × 10^308

IEEE 754 Double (8 bytes):
  ┌──────┬─────────────┬──────────────────────────────────────┐
  │ Sign │  Exponent   │           Mantissa                    │
  │ 1bit │  11 bits    │           52 bits                     │
  └──────┴─────────────┴──────────────────────────────────────┘

WARNING: Floating point is INEXACT.
  0.1 + 0.2 = 0.30000000000000004 (not 0.3)
  Never use FLOAT/DOUBLE for money. Use NUMERIC/DECIMAL instead.
```

### NUMERIC / DECIMAL (Arbitrary Precision)

```
NUMERIC(precision, scale):
  - precision = total number of digits
  - scale = digits after decimal point
  - NUMERIC(10, 2) stores up to 99999999.99

PostgreSQL encoding:
  Stored as an array of base-10000 "digits" (int16 values).
  NUMERIC(10,2) value 12345678.90:
    Sign: positive
    Weight: 1 (number of base-10000 digits before decimal)
    Digits: [1234, 5678, 9000]

  ┌───────────────────────────────────────────┐
  │ Header  │ ndigits │ weight │ sign │ dscale│
  │ (varlena)│ (2B)    │ (2B)   │ (2B) │ (2B) │
  ├─────────┴─────────┴────────┴──────┴──────┤
  │ digit[0]=1234 │ digit[1]=5678 │ digit[2]=9000 │
  └───────────────┴───────────────┴───────────────┘

  Each digit is 2 bytes, stores 0-9999.
  NUMERIC is EXACT but 5-10x slower than integer/float for math.

InnoDB encoding:
  Stores DECIMAL as packed BCD (Binary-Coded Decimal).
  Each decimal digit takes 4 bits (half a byte).
  DECIMAL(10,2) = 5 bytes for integer part + 1 byte for fractional = 6 bytes total.
  More compact than PostgreSQL's representation.
```

### String Encoding

```
Character Types:
  CHAR(N)    - Fixed-length, space-padded to N characters
  VARCHAR(N) - Variable-length, up to N characters
  TEXT       - Variable-length, unlimited (PostgreSQL, MySQL)

Storage:
  PostgreSQL:
    CHAR(10) "hello"  → stored as "hello     " (10 bytes + varlena header)
    VARCHAR(100) "hello" → stored as "hello" (5 bytes + varlena header)
    TEXT "hello" → identical to VARCHAR internally (5 bytes + varlena header)
    All three use varlena format. CHAR is space-padded on output only.

  InnoDB:
    CHAR(10) → always 10 bytes on disk (fixed allocation)
    VARCHAR(100) "hello" → 1-byte length prefix + 5 bytes = 6 bytes
    (length prefix is 2 bytes if max length > 255 bytes)

  Encoding: Stored in the column's CHARACTER SET encoding.
    - PostgreSQL: server encoding (usually UTF-8)
    - MySQL: per-column charset (utf8mb4 = 4 bytes/char max)

  Collation: Determines sort order. Affects index ordering.
    - "a" < "B" in case-sensitive; "a" < "B" or "a" > "B"
      depending on collation.
    - PostgreSQL: uses OS locale (libc) or ICU provider
    - MySQL: collation is per-column (e.g., utf8mb4_unicode_ci)
```

### Date and Time Encoding

```
DATE:
  PostgreSQL: 4 bytes, days since 2000-01-01 (Julian day offset)
              2024-06-15 → stored as integer 8932
  MySQL:      3 bytes, packed as YYYY×16×32 + MM×32 + DD
  SQL Server: 3 bytes, days since 0001-01-01

TIMESTAMP (without time zone):
  PostgreSQL: 8 bytes, microseconds since 2000-01-01 00:00:00
              Stored as int64. Range: 4713 BC to 294276 AD.
  MySQL:      4 bytes (TIMESTAMP) = seconds since Unix epoch
              8 bytes (DATETIME) = packed YYYY-MM-DD HH:MM:SS

TIMESTAMP WITH TIME ZONE:
  PostgreSQL: 8 bytes, same as TIMESTAMP but interpreted as UTC.
              The timezone is NOT stored -- it's converted to UTC
              on input and converted back to session timezone on output.
  Note: this is a common misconception. TIMESTAMPTZ does NOT
        store the timezone. It stores UTC microseconds.

INTERVAL:
  PostgreSQL: 16 bytes (months: int32, days: int32, microseconds: int64)
              Months and days stored separately because "1 month" ≠ "30 days"
              (months vary 28-31 days) and "1 day" ≠ "24 hours" (DST).
```

### Boolean, UUID, JSON

```
BOOLEAN:
  PostgreSQL: 1 byte (0x00 = false, 0x01 = true)
              Yes, a full byte for 1 bit of information.
              Alignment rules make sub-byte storage impractical.
  MySQL:      TINYINT(1), 1 byte. BOOLEAN is just an alias.

UUID:
  PostgreSQL: 16 bytes, stored as raw binary.
              Display: 550e8400-e29b-41d4-a716-446655440000
              Stored:  [55 0e 84 00 e2 9b 41 d4 a7 16 44 66 55 44 00 00]

  As a primary key:
    UUID v4 (random): causes massive B-Tree fragmentation.
      Random values scatter inserts across all leaf pages → no locality.
    UUID v7 (time-ordered, RFC 9562): monotonically increasing prefix.
      Inserts append to the right edge of the B-Tree → sequential.
      Highly recommended over v4 for database primary keys.

JSON / JSONB:
  PostgreSQL JSON:  stored as raw text. Parsed on every access.
  PostgreSQL JSONB: stored as decomposed binary format:
    ┌──────────────────────────────────────────────────────┐
    │ Header (type: object, num_pairs: 3)                   │
    │ JEntry[0]: key offset, value offset                   │
    │ JEntry[1]: key offset, value offset                   │
    │ JEntry[2]: key offset, value offset                   │
    │ Key data: "age\0email\0name\0"                        │
    │ Value data: (numeric)30 | (string)"a@ex.com" | ...   │
    └──────────────────────────────────────────────────────┘
    Keys are sorted → binary search on key lookup.
    No reparsing needed. Supports GIN indexing.

  MySQL JSON (5.7+): similar binary format, keys sorted.
```

---

## 3. Fixed-Length vs Variable-Length Data

### Fixed-Length Column Storage

Columns with fixed-size types (INTEGER, BIGINT, DATE, FLOAT, CHAR(N), BOOLEAN) occupy the exact same number of bytes in every row. This enables direct offset calculation.

```
Fixed-length row example:
  Table: sensors (id INT, temp FLOAT, reading_date DATE, active BOOLEAN)

  Column offsets (after tuple header):
    id:            offset 0,  4 bytes
    temp:          offset 4,  4 bytes
    reading_date:  offset 8,  4 bytes   (PG stores DATE as 4 bytes)
    active:        offset 12, 1 byte

  Accessing temp for row at byte position P:
    → just read 4 bytes at P + 4. O(1), no parsing needed.

  ┌────────────┬────────────┬──────────────┬────────┐
  │ id (4B)    │ temp (4B)  │ date (4B)    │ act(1B)│
  ├────────────┼────────────┼──────────────┼────────┤
  │ 00 00 00 01│ 41 C8 00 00│ 00 00 22 E4  │ 01     │
  └────────────┴────────────┴──────────────┴────────┘
```

### Variable-Length Column Storage (varlena)

Columns with variable-size types (VARCHAR, TEXT, BYTEA, NUMERIC, JSONB) use a length-prefixed format. PostgreSQL calls this **varlena** (variable-length attributes).

```
PostgreSQL varlena format:

  SHORT varlena (value ≤ 126 bytes):
  ┌──────────────┬───────────────────────────────┐
  │ 1-byte header│ Data (up to 126 bytes)         │
  │ (length << 1 │                                │
  │  | 0x01)     │                                │
  └──────────────┴───────────────────────────────┘
  The low bit = 1 signals "short header."
  Length is stored in the upper 7 bits.

  LONG varlena (value > 126 bytes):
  ┌──────────────────┬───────────────────────────────────┐
  │ 4-byte header    │ Data (up to ~1 GB)                 │
  │ (total length    │                                    │
  │  including hdr)  │                                    │
  └──────────────────┴───────────────────────────────────┘
  The low 2 bits of the first byte encode the type:
    00 = 4-byte header, uncompressed
    10 = 4-byte header, compressed (TOAST inline compressed)

  Example: VARCHAR value "hello world" (11 bytes)
  ┌────┬─────────────────────────┐
  │ 17 │ h e l l o   w o r l d   │  (header = (12 << 1) | 0x01 = 0x19 = 25)
  └────┴─────────────────────────┘  Wait, let me re-do:
                                     length including header = 12 bytes
                                     short header = (12 << 1) | 1 = 25 = 0x19

InnoDB variable-length column storage:

  ┌──────────────────────────────────────────────────┐
  │ Record header                                      │
  │ ┌──────────────────────────────────────────┐      │
  │ │ Variable-length field lengths (reverse)   │      │
  │ │ [len_col3] [len_col2] [len_col1]         │      │
  │ │ 1 byte if max_len ≤ 255                  │      │
  │ │ 2 bytes if max_len > 255                  │      │
  │ └──────────────────────────────────────────┘      │
  │ ┌──────────────────────────────────────────┐      │
  │ │ NULL flags (1 bit per nullable column)    │      │
  │ └──────────────────────────────────────────┘      │
  │ ┌──────────────────────────────────────────┐      │
  │ │ Record header (5 bytes)                   │      │
  │ └──────────────────────────────────────────┘      │
  └──────────────────────────────────────────────────┘
  Then column data in column order.

  Key difference from PostgreSQL: InnoDB stores variable-length
  field lengths in REVERSE column order before the record,
  so the record header is immediately adjacent to the data.
```

### Alignment and Padding

CPUs access memory most efficiently when data is aligned to its natural boundary (4-byte values at 4-byte boundaries, 8-byte values at 8-byte boundaries). Databases add padding bytes to enforce alignment.

```
PostgreSQL alignment rules:

  Type        Size    Alignment
  ────────    ─────   ─────────
  bool        1 byte  1-byte (char)
  int2        2 bytes 2-byte (short)
  int4        4 bytes 4-byte (int)
  int8        8 bytes 8-byte (double)
  float4      4 bytes 4-byte (int)
  float8      8 bytes 8-byte (double)
  text/varchar variable 4-byte (int) [due to varlena 4-byte header]
                        OR 1-byte (short varlena)
  numeric     variable 4-byte (int)

  Table: t (a bool, b int8, c int2, d int4)

  Naive layout:   [a: 1B] [pad: 7B] [b: 8B] [c: 2B] [pad: 2B] [d: 4B]
  Total: 24 bytes (8 bytes of padding!)

  Reordered:      [b: 8B] [d: 4B] [c: 2B] [a: 1B] [pad: 1B]
  Total: 16 bytes (1 byte of padding)

  PostgreSQL does NOT automatically reorder columns.
  Column order in CREATE TABLE determines physical storage order.

  Practical advice: declare columns in descending alignment order
  (int8, float8, int4, float4, int2, bool) to minimize padding.
  For most applications the savings are negligible, but for
  billion-row tables it can save gigabytes.
```

---

## 4. NULL Representation

### NULL Bitmaps

NULLs are not stored as data values. Instead, a **null bitmap** in the row header marks which columns are NULL. If a column is NULL, no space is used for its data.

```
PostgreSQL NULL bitmap:

  Tuple header includes t_infomask flag HEAP_HASNULL.
  If set, a null bitmap follows the fixed tuple header (23 bytes).

  Bitmap: 1 bit per column. Bit = 1 means NOT NULL, 0 means NULL.

  Example: table with 8 columns, columns 3 and 7 are NULL:
  ┌─────────────────────┬──────────────┬──────────────────────────┐
  │ Tuple header (23B)  │ Null bitmap  │ Column data               │
  │                     │ [1 1 0 1 1 1 │ (col1)(col2)(col4)(col5) │
  │                     │  0 1]        │ (col6)(col8)              │
  │                     │ = 0xDB       │ cols 3,7 not stored       │
  └─────────────────────┴──────────────┘──────────────────────────┘

  Size of null bitmap: ceil(num_columns / 8) bytes.
  For 8 columns: 1 byte
  For 20 columns: 3 bytes
  For 100 columns: 13 bytes

  The bitmap is ONLY present if at least one column in the table
  is defined as nullable. If ALL columns are NOT NULL, the bitmap
  is omitted entirely, saving ceil(N/8) bytes per row.

InnoDB NULL flags:

  Stored as part of the variable-length field lengths area,
  before the record header. 1 bit per nullable column.
  Only nullable columns consume a bit. NOT NULL columns are skipped.

  ┌───────────────┬────────────┬──────────┬──────────┐
  │ Var-len lens  │ NULL flags │ Rec hdr  │ Col data │
  │ (reversed)    │ (1 bit per │ (5 bytes)│          │
  │               │ nullable)  │          │          │
  └───────────────┴────────────┴──────────┴──────────┘
```

### Impact of NOT NULL Constraints

```
Why NOT NULL saves space (PostgreSQL):

  All columns nullable (default):
    - Null bitmap ALWAYS present: ceil(N_cols / 8) bytes per row
    - Even if no values are actually NULL

  All columns NOT NULL:
    - Null bitmap OMITTED entirely: saves ceil(N_cols / 8) bytes per row
    - For a table with 16 columns: saves 2 bytes per row
    - For 100M rows: saves 200 MB

  Mixed (some nullable, some not):
    - Bitmap is present (includes ALL columns)
    - NOT NULL columns always have bit=1 (no saving in bitmap)
    - But: declaring NOT NULL still skips data storage for nulls
      and enables query optimizer optimizations

  Recommendation: Always declare NOT NULL when the column
  genuinely cannot be null. It saves space and helps the optimizer.
```

---

## 5. TOAST and Overflow Pages

### The Problem: Rows That Don't Fit on a Page

A single page is 8 KB (PostgreSQL) or 16 KB (InnoDB). But a single row with TEXT or BYTEA columns can be megabytes. The row must either be compressed, stored off-page, or both.

### PostgreSQL: TOAST (The Oversized-Attribute Storage Technique)

TOAST is PostgreSQL's mechanism for handling large attribute values. It kicks in when a tuple would exceed roughly 2 KB (one quarter of the 8 KB page).

```
TOAST STRATEGIES (per column):

  ┌──────────────┬──────────────────────────────────────────────────┐
  │ Strategy      │ Behavior                                         │
  ├──────────────┼──────────────────────────────────────────────────┤
  │ PLAIN         │ No TOAST. Value must fit inline.                 │
  │               │ Used for fixed-length types (int, float, etc.)  │
  ├──────────────┼──────────────────────────────────────────────────┤
  │ EXTENDED      │ Try compression first. If still too big,        │
  │ (default for  │ store externally in TOAST table.                │
  │  text, bytea) │ Allows both compression and external storage.   │
  ├──────────────┼──────────────────────────────────────────────────┤
  │ EXTERNAL      │ Store externally if too big, but do NOT         │
  │               │ compress. Faster for already-compressed data    │
  │               │ (images, PDFs) where compression wastes CPU.    │
  ├──────────────┼──────────────────────────────────────────────────┤
  │ MAIN          │ Try compression first. Store externally only    │
  │               │ as last resort (if row still won't fit).        │
  │               │ Prefers inline compressed storage.              │
  └──────────────┴──────────────────────────────────────────────────┘

TOAST Mechanism:

  Original row (too large for page):
  ┌────────────────────────────────────────────────────────────────┐
  │ id=1 │ name="Alice" │ bio="<5 KB text about Alice...>"        │
  │                      Total: ~5.2 KB (exceeds ~2 KB threshold) │
  └────────────────────────────────────────────────────────────────┘

  After TOAST (EXTENDED strategy):
  Step 1: Compress bio → pglz compressed to ~1.8 KB → fits inline!
  ┌──────────────────────────────────────────────────────────────┐
  │ id=1 │ name="Alice" │ bio=<compressed, 1.8 KB inline>       │
  └──────────────────────────────────────────────────────────────┘

  If compression doesn't shrink enough (say bio is 50 KB):
  Step 2: Store in TOAST table

  Main heap row:
  ┌─────────────────────────────────────────────────────────────┐
  │ id=1 │ name="Alice" │ bio=<TOAST pointer: 18 bytes>         │
  │                       (chunk_id=1234, toast_relid=99887)    │
  └─────────────────────────────────────────────────────────────┘

  TOAST table (pg_toast.pg_toast_<table_oid>):
  ┌──────────────────────────────────────────────────────────────┐
  │ chunk_id=1234, chunk_seq=0, chunk_data=[first 2000 bytes]   │
  │ chunk_id=1234, chunk_seq=1, chunk_data=[next 2000 bytes]    │
  │ chunk_id=1234, chunk_seq=2, chunk_data=[remaining bytes]    │
  └──────────────────────────────────────────────────────────────┘

  The TOAST table has an index on (chunk_id, chunk_seq) for
  efficient retrieval. Each chunk is ~2 KB.
```

**Key implication**: `SELECT id, name FROM users` never touches the TOAST table, even if `bio` is 50 MB. TOAST values are only fetched when the column is actually accessed. This is a major performance feature for tables with large TEXT/JSONB columns.

### InnoDB: Overflow Pages

InnoDB uses a different approach for large rows. The behavior depends on the row format:

```
InnoDB Row Formats:

  COMPACT / REDUNDANT (older):
    Variable-length columns > 768 bytes:
    ┌──────────────────────────────────────────────────────┐
    │ Row: [col1] [col2] [first 768 bytes of col3]         │
    │                    [20-byte pointer to overflow page] │
    └──────────────────────────────────────────────────────┘
    The first 768 bytes are stored inline (prefix).
    Rest goes to overflow pages linked by pointers.

  DYNAMIC (default since MySQL 5.7):
    Large columns: only a 20-byte pointer stored inline.
    ┌──────────────────────────────────────────────────────┐
    │ Row: [col1] [col2] [20-byte pointer to overflow]     │
    └──────────────────────────────────────────────────────┘
    The entire value lives on overflow pages.
    More rows fit per page → better B-Tree fan-out.

  COMPRESSED:
    Like DYNAMIC, but the entire page is compressed with zlib.
    Overflow pages are also compressed.

  Overflow page chain:
  ┌──────────┐    ┌──────────┐    ┌──────────┐
  │ Overflow │───►│ Overflow │───►│ Overflow │
  │ Page 1   │    │ Page 2   │    │ Page 3   │
  │ 16 KB    │    │ 16 KB    │    │ 8 KB     │
  └──────────┘    └──────────┘    └──────────┘
```

### SQL Server: Row-Overflow and LOB Storage

```
SQL Server stores data in three allocation units:

  IN-ROW DATA (≤ 8060 bytes per row):
  ┌────────────────────────────────────────────────┐
  │ Normal data page. Row fits entirely here.       │
  └────────────────────────────────────────────────┘

  ROW-OVERFLOW DATA (varchar/varbinary > 8060):
  ┌────────────────────────────────────────────────┐
  │ In-row: 24-byte pointer to overflow page        │
  │ Overflow page: rest of the column value         │
  └────────────────────────────────────────────────┘

  LOB DATA (text, ntext, image, varchar(max), xml):
  ┌────────────────────────────────────────────────┐
  │ In-row: 16-byte pointer to LOB root structure   │
  │ LOB pages: B-Tree of chunks for the LOB value  │
  └────────────────────────────────────────────────┘
```

---

## 6. Data Compression

### Page-Level Compression

Some databases compress entire pages transparently.

```
InnoDB Transparent Page Compression (innodb_compression):

  ROW_FORMAT=COMPRESSED:
  1. Fill a 16 KB page with rows as usual
  2. Compress entire page with zlib (1-9 level)
  3. Store compressed page in a smaller on-disk block
  4. In buffer pool: both compressed + uncompressed copies exist
     (uncompressed for reads, compressed for writing to disk)

  innodb_page_compression (Punch Hole, MySQL 5.7+):
  1. Fill a 16 KB page normally
  2. Compress with LZ4 or Zstd
  3. Write 16 KB page but "punch holes" in unused trailing space
  4. Filesystem reclaims the holed space (requires sparse file support)
  5. Buffer pool only keeps uncompressed version

SQL Server Page Compression (3 phases):
  1. ROW compression: compact integer storage, remove trailing zeros
  2. PREFIX compression: factor out common prefix per column per page
  3. DICTIONARY compression: replace repeated values with short tokens

  Example page with city column values:
    ["San Francisco", "San Jose", "San Diego", "San Francisco"]
    After prefix: prefix="San " + ["Francisco", "Jose", "Diego", "Francisco"]
    After dictionary: "Francisco"=0, "Jose"=1, "Diego"=2 → [0, 1, 2, 0]
```

### Column-Store Compression Techniques

Columnar engines achieve much higher compression ratios because same-type values cluster together. These are the core techniques:

```
DICTIONARY ENCODING:
  Original:    ["USA", "USA", "Canada", "USA", "France", "Canada"]
  Dictionary:  {0: "USA", 1: "Canada", 2: "France"}
  Encoded:     [0, 0, 1, 0, 2, 1]  ← integers instead of strings

  Savings: 6 strings × avg 5 bytes = 30 bytes
         → 6 × 1 byte + 14 bytes dict = 20 bytes (33% savings)
  At scale with millions of rows and few distinct values: 90%+ savings.

RUN-LENGTH ENCODING (RLE):
  Original:    [1, 1, 1, 1, 2, 2, 3, 3, 3, 3, 3]
  RLE:         [(1, 4), (2, 2), (3, 5)]  ← (value, count)

  Most effective on sorted columns or columns with long runs
  of repeated values (e.g., date column in time-series data).

DELTA ENCODING:
  Original:    [1000, 1001, 1003, 1004, 1007, 1010]
  Deltas:      [1000, 1, 2, 1, 3, 3]

  Then the small delta values can be bit-packed:
  [1000 as base] + [1, 2, 1, 3, 3] stored in 2 bits each
  Perfect for timestamps, auto-increment IDs, sorted columns.

FRAME-OF-REFERENCE (FOR):
  Original:    [10042, 10047, 10050, 10031, 10055]
  Reference:   10031 (minimum value)
  Offsets:     [11, 16, 19, 0, 24]  ← max offset = 24, fits in 5 bits

  Store: reference (4 bytes) + N values × 5 bits
  vs original: N × 4 bytes

BIT-PACKING:
  Values [3, 1, 4, 1, 5, 2] — max value 5, needs 3 bits each.
  Pack 3 bits per value instead of 32 bits:
  6 values × 3 bits = 18 bits = 3 bytes (vs 24 bytes uncompressed)

PREFIX COMPRESSION (for sorted string columns):
  ["apple", "application", "apply", "approach"]
  → [("apple", 5), ("ication", 5), ("y", 4), ("roach", 3)]
     prefix_length means "share first N chars with previous"
```

### Compression Ratio Examples

| Data Type / Pattern | Technique | Typical Ratio |
|--------------------|-----------|---------------|
| Status codes (5 distinct values, 10M rows) | Dictionary | 20-50:1 |
| Timestamps (monotonic, 1-sec intervals) | Delta + bit-pack | 8-15:1 |
| Country codes (200 distinct, 100M rows) | Dictionary | 10-30:1 |
| Random UUIDs | LZ4/Zstd (general purpose) | 1.0-1.2:1 (barely compressible) |
| JSON documents (repetitive keys) | LZ4/Zstd | 3-8:1 |
| Sorted integer IDs | Delta + RLE | 50-200:1 |
| Boolean flags | Bit-packing | 8:1 (8 bools in 1 byte) |

### General-Purpose Compression Algorithms

| Algorithm | Speed (compress) | Speed (decompress) | Ratio | Used By |
|-----------|-----------------|---------------------|-------|---------|
| LZ4 | Very fast (~700 MB/s) | Very fast (~4 GB/s) | Low-moderate | RocksDB, ClickHouse, DuckDB |
| Zstd | Fast (~400 MB/s) | Fast (~1.5 GB/s) | High | RocksDB, PostgreSQL 15+ (WAL), Parquet |
| Snappy | Very fast (~500 MB/s) | Very fast (~1.5 GB/s) | Low-moderate | Google Bigtable, LevelDB |
| zlib | Slow (~100 MB/s) | Moderate (~400 MB/s) | High | InnoDB ROW_FORMAT=COMPRESSED, older systems |
| pglz | Moderate | Moderate | Moderate | PostgreSQL TOAST (built-in, being replaced by LZ4) |

---

## 7. Row Versioning Formats (MVCC Storage)

Multi-Version Concurrency Control (MVCC) requires storing multiple versions of each row so that readers see a consistent snapshot without blocking writers. How versions are physically stored varies dramatically across databases.

### PostgreSQL: Heap-Based Multi-Version

PostgreSQL stores **all row versions directly in the main heap**. Both old and new versions coexist as separate physical tuples.

```
PostgreSQL MVCC -- UPDATE creates a new tuple:

  UPDATE users SET age = 31 WHERE id = 1;  (txn_id = 500)

  BEFORE update -- Page 5:
  ┌──────────────────────────────────────────────────────────────┐
  │ Tuple A (live):                                               │
  │   t_xmin = 100  (created by txn 100)                         │
  │   t_xmax = 0    (not deleted)                                │
  │   t_ctid = (5,1) (points to self -- this IS the latest)     │
  │   data:  {id=1, name="Alice", age=30}                       │
  └──────────────────────────────────────────────────────────────┘

  AFTER update -- Page 5 (may be same page or different):
  ┌──────────────────────────────────────────────────────────────┐
  │ Tuple A (now dead):                                           │
  │   t_xmin = 100                                               │
  │   t_xmax = 500  (killed by txn 500)                          │
  │   t_ctid = (5,3) (points to new version ──────┐)            │
  │   data:  {id=1, name="Alice", age=30}          │             │
  │                                                 │             │
  │ Tuple B (new live version):              ◄──────┘             │
  │   t_xmin = 500  (created by txn 500)                         │
  │   t_xmax = 0    (not deleted)                                │
  │   t_ctid = (5,3) (points to self)                            │
  │   data:  {id=1, name="Alice", age=31}                       │
  └──────────────────────────────────────────────────────────────┘

  Version chain: Tuple A → Tuple B (via t_ctid pointers)

  Readers at snapshot < 500 see Tuple A.
  Readers at snapshot >= 500 see Tuple B.
  VACUUM eventually removes Tuple A when no snapshot can see it.
```

**Consequences**:
- Tables grow ("bloat") because dead tuples consume space until VACUUM reclaims them
- Indexes also contain pointers to dead tuples → index bloat
- VACUUM is critical for space reclamation and performance
- HOT (Heap-Only Tuple) updates can avoid index updates if the new tuple fits on the same page and no indexed columns changed

### InnoDB: Undo Log Approach

InnoDB stores only the **latest version in-place** in the clustered index. Older versions are reconstructed from the undo log.

```
InnoDB MVCC -- UPDATE modifies in-place + writes undo:

  UPDATE users SET age = 31 WHERE id = 1;  (txn_id = 500)

  Clustered Index (B+Tree leaf page):
  ┌──────────────────────────────────────────────────────────────┐
  │ Record (CURRENT version):                                     │
  │   trx_id = 500                                               │
  │   roll_ptr = ──────────────────────────┐  (points to undo)  │
  │   data: {id=1, name="Alice", age=31}   │                     │
  └────────────────────────────────────────┼─────────────────────┘
                                           │
  Undo Log (undo tablespace):             │
  ┌────────────────────────────────────────▼─────────────────────┐
  │ Undo record:                                                   │
  │   old_trx_id = 100                                            │
  │   old_roll_ptr = NULL (no older version)                      │
  │   undo data: {age = 30}  (just the changed columns!)         │
  └──────────────────────────────────────────────────────────────┘

  Reader at snapshot < 500:
  1. Read current record: trx_id=500 > snapshot → not visible
  2. Follow roll_ptr to undo log
  3. Reconstruct: apply undo → {id=1, name="Alice", age=30}
  4. Check old_trx_id=100 < snapshot → visible! Return this version.

  After many updates, the undo chain can be long:
  Current (trx 500) → undo (trx 400) → undo (trx 300) → undo (trx 100)

  Purge thread cleans old undo records when no active snapshot
  needs them (similar to PostgreSQL's VACUUM but for undo logs).
```

**Consequences**:
- Main table does not bloat (in-place update)
- No index updates needed for non-key column changes
- Long undo chains slow down reads for old snapshots (undo traversal)
- "History list length" growing means purge is falling behind

### SQL Server: TempDB Version Store

```
SQL Server (Read Committed Snapshot Isolation):

  Main data page: stores CURRENT version only.
  Version store: old versions go to tempdb.

  ┌───────────────────────┐
  │ Data Page              │
  │ Row: {id=1, age=31}   │──── 14-byte version pointer ────┐
  │ trx_seq = 500         │                                  │
  └───────────────────────┘                                  │
                                                             ▼
  ┌───────────────────────────────────────────────────────────┐
  │ TempDB Version Store                                       │
  │ Version: {id=1, age=30}                                   │
  │ trx_seq = 100                                              │
  │ prev_version_ptr = NULL                                    │
  └───────────────────────────────────────────────────────────┘

  Pros: tempdb is usually fast (often on SSD/RAM disk)
  Cons: tempdb can fill up under heavy update load;
        each versioned row adds 14 bytes to the main row
```

### MVCC Storage Comparison

| Aspect | PostgreSQL (Heap) | InnoDB (Undo Log) | SQL Server (TempDB) |
|--------|-------------------|--------------------|--------------------|
| Where old versions live | Main heap (same pages) | Undo tablespace | TempDB |
| Table bloat from updates | Yes (requires VACUUM) | No | No |
| Index bloat from updates | Yes | No (if non-key update) | No |
| Cost of reading old version | Direct (just read old tuple) | Undo chain traversal | TempDB read |
| Long-running txn impact | Prevents VACUUM cleanup | Long undo chains | TempDB grows |
| Space overhead per row | Full duplicate tuple | 7-byte roll_ptr + undo record | 14-byte version tag |
| Background cleanup | VACUUM / autovacuum | Purge threads | Background task |

---

## 8. Encoding Schemes in Columnar / Analytical Engines

### Parquet File Format

Apache Parquet is the dominant columnar file format for data lakes. Understanding its encoding is essential for analytical workloads.

```
PARQUET FILE STRUCTURE:

  ┌──────────────────────────────────────────────────────────────┐
  │ Magic: "PAR1" (4 bytes)                                      │
  ├──────────────────────────────────────────────────────────────┤
  │ ROW GROUP 0                                                   │
  │ ┌──────────────────────────────────────────────────────────┐ │
  │ │ Column Chunk: "id"                                        │ │
  │ │ ┌──────────┬──────────┬──────────┐                       │ │
  │ │ │ Data Page│ Data Page│ Dict Page│                       │ │
  │ │ │ 0        │ 1        │ (opt.)   │                       │ │
  │ │ └──────────┴──────────┴──────────┘                       │ │
  │ │ Column Chunk: "name"                                      │ │
  │ │ ┌──────────┬──────────┬──────────┐                       │ │
  │ │ │ Data Page│ Data Page│ Dict Page│                       │ │
  │ │ └──────────┴──────────┴──────────┘                       │ │
  │ │ Column Chunk: "age"                                       │ │
  │ │ ┌──────────┬──────────┐                                  │ │
  │ │ │ Data Page│ Data Page│                                  │ │
  │ │ └──────────┴──────────┘                                  │ │
  │ └──────────────────────────────────────────────────────────┘ │
  ├──────────────────────────────────────────────────────────────┤
  │ ROW GROUP 1                                                   │
  │ ┌──────────────────────────────────────────────────────────┐ │
  │ │ (same structure as above)                                 │ │
  │ └──────────────────────────────────────────────────────────┘ │
  ├──────────────────────────────────────────────────────────────┤
  │ FOOTER                                                        │
  │ ┌──────────────────────────────────────────────────────────┐ │
  │ │ Schema (column names, types, nesting)                     │ │
  │ │ Row group metadata (offsets, sizes, row counts)           │ │
  │ │ Column chunk metadata per column per row group:           │ │
  │ │   - encoding (PLAIN, DELTA, RLE_DICTIONARY, etc.)        │ │
  │ │   - compression (SNAPPY, ZSTD, LZ4, GZIP)               │ │
  │ │   - statistics: min, max, null_count, distinct_count     │ │
  │ │   - total compressed/uncompressed size                    │ │
  │ └──────────────────────────────────────────────────────────┘ │
  │ Footer length (4 bytes)                                      │
  │ Magic: "PAR1" (4 bytes)                                      │
  └──────────────────────────────────────────────────────────────┘

  Typical row group size: 128 MB (configurable).
  A 10 GB file ≈ ~80 row groups.

  Reading "SELECT AVG(age) FROM t":
  1. Read footer (seek to end of file)
  2. For each row group: read only the "age" column chunk
  3. Skip "id" and "name" column chunks entirely → huge I/O savings
```

### Repetition and Definition Levels (Nested Data)

Parquet handles nested/repeated fields (like arrays, maps, nested structs) using Dremel's encoding scheme:

```
Schema:  message Document {
           required int64 id;
           repeated group links {
             optional string url;
           }
         }

Data:    {id: 1, links: [{url: "a.com"}, {url: null}, {url: "b.com"}]}
         {id: 2, links: []}
         {id: 3, links: [{url: "c.com"}]}

Column "links.url" encoding:

  Value     Rep Level  Def Level  Meaning
  ─────     ─────────  ─────────  ────────
  "a.com"   0          2          New record, value present
  null      1          1          Repeated (same record), null value
  "b.com"   1          2          Repeated (same record), value present
  (nothing) 0          0          New record, empty list (no links at all)
  "c.com"   0          2          New record, value present

  Rep level: 0 = new top-level record, 1 = repeated within parent
  Def level: 0 = list not present, 1 = list element present but value null,
             2 = value present

  This eliminates the need for structural markers like brackets,
  enabling pure columnar storage even for nested data.
```

### Zone Maps / Min-Max Statistics

Every row group and every data page stores column statistics. This enables **predicate pushdown** -- skipping entire chunks of data without reading them.

```
Row Group statistics for "age" column:

  Row Group 0:  min=18,  max=65,   null_count=0
  Row Group 1:  min=22,  max=45,   null_count=3
  Row Group 2:  min=50,  max=92,   null_count=1

  Query: SELECT * FROM users WHERE age < 20

  Row Group 0: min=18 < 20 → MUST SCAN (might have matching rows)
  Row Group 1: min=22 ≥ 20 → SKIP ENTIRELY (no rows can match)
  Row Group 2: min=50 ≥ 20 → SKIP ENTIRELY

  Result: only 1 of 3 row groups is read. 66% I/O eliminated.

  This is also called "zone maps" (Oracle), "data skipping" (Databricks),
  or "min/max indexes" (ClickHouse).
```

### ORC File Format

Apache ORC (Optimized Row Columnar) is Hive's native format, similar to Parquet but with some differences:

```
ORC vs Parquet:

  ┌──────────────────┬────────────────────┬────────────────────┐
  │ Feature          │ Parquet              │ ORC                  │
  ├──────────────────┼────────────────────┼────────────────────┤
  │ Origin           │ Cloudera + Twitter │ Hortonworks (Hive) │
  │ Row group term   │ Row Group          │ Stripe             │
  │ Default size     │ 128 MB             │ 256 MB             │
  │ Index            │ Min/max per page   │ Min/max + bloom    │
  │                  │                    │ filter per stripe  │
  │ Nested data      │ Dremel (rep/def)   │ Struct decomposition│
  │ Compression      │ Per-page           │ Per-stream         │
  │ ACID support     │ No (immutable)     │ Yes (Hive ACID)    │
  │ Ecosystem        │ Broader (Spark,    │ Hive, Presto       │
  │                  │ all engines)       │                    │
  └──────────────────┴────────────────────┴────────────────────┘
```

### Apache Arrow (In-Memory Columnar)

Arrow is not a file format but an **in-memory columnar layout** standard. It is designed for zero-copy reads and SIMD-friendly processing.

```
Arrow columnar layout (in memory):

  Column "age" (Int32, 6 values, 1 null):
  ┌──────────────────────────────────────────────┐
  │ Validity bitmap: [1, 1, 0, 1, 1, 1]          │  ← 1 byte (bit-packed)
  │                  row 2 is NULL                │
  ├──────────────────────────────────────────────┤
  │ Values buffer:  [30][25][??][35][28][22]      │  ← 6 × 4 bytes = 24 bytes
  │                       ↑ garbage, masked by   │
  │                         validity bitmap       │
  └──────────────────────────────────────────────┘

  Key properties:
  - Fixed-width values are contiguous → SIMD-friendly (process 4-8 at a time)
  - No deserialization needed → zero-copy between processes
  - Standardized layout → data can be shared between
    DuckDB, Pandas, Spark, Polars, DataFusion without copying
  - Variable-length (strings): offsets array + data buffer
    offsets: [0, 5, 8, 15, 19, 23, 27]
    data:    "AliceBobCharleDaveJohnMary"
```

### DuckDB's Internal Encoding

DuckDB uses a hybrid approach optimized for analytical queries on a single machine:

```
DuckDB storage:

  - Row groups of ~122,880 rows (fits in L2 cache per column segment)
  - Per-column segments within each row group
  - Automatic encoding selection per segment:
    • Constant: all values the same → store once
    • Dictionary: few distinct values → dict + codes
    • RLE: sorted/clustered → run-length
    • BitPacking: small integers → packed bits
    • FOR + BitPacking: close-range integers
    • Uncompressed: if nothing helps

  - Lightweight compression (LZ4/Zstd) on top of encoding
  - Morsel-driven parallelism: each thread processes a
    row group independently, no locks needed

  Result: DuckDB often matches or beats dedicated columnar
  databases for single-machine analytical queries.
```

---
