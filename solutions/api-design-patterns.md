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

**Intent:** Handle large datasets efficiently.

#### Offset Pagination
```http
GET /api/v1/users?offset=100&limit=20

Response:
{
  "data": [...],
  "pagination": {
    "offset": 100,
    "limit": 20,
    "total": 1543
  }
}
```

#### Cursor Pagination (Preferred for large datasets)
```http
GET /api/v1/events?cursor=eyJpZCI6MTAwfQ&limit=20

Response:
{
  "data": [...],
  "pagination": {
    "next_cursor": "eyJpZCI6MTIwfQ",
    "has_more": true
  }
}
```

**Stripe Standard:**
```json
{
  "object": "list",
  "url": "/v1/customers",
  "has_more": true,
  "data": [...],
  "next_page": "https://api.stripe.com/v1/customers?starting_after=cus_123"
}
```

**Google Standard:**
```json
{
  "items": [...],
  "nextPageToken": "CAESEA...",
  "totalSize": 1543
}
```

#### Keyset Pagination (Best performance)
```http
GET /api/v1/logs?after_id=log_abc&limit=100

Response:
{
  "data": [...],
  "pagination": {
    "first_id": "log_def",
    "last_id": "log_xyz",
    "has_more": true
  }
}
```

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

# Pattern: Always paginate collections
GET /api/users?page_size=20&page_token=abc

{
  "data": [...],
  "pagination": {
    "total_count": 1000000,
    "page_size": 20,
    "next_page_token": "def456"
  }
}
```

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
