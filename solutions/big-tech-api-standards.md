# Big Tech API Standards Reference

Detailed API design standards from industry leaders with practical implementation examples.

---

## Table of Contents

1. [Google API Design Guide](#1-google-api-design-guide)
2. [Microsoft REST API Guidelines](#2-microsoft-rest-api-guidelines)
3. [Amazon Web Services](#3-amazon-web-services)
4. [Stripe API Design](#4-stripe-api-design)
5. [GitHub API Design](#5-github-api-design)
6. [Slack API Design](#6-slack-api-design)
7. [Netflix API Practices](#7-netflix-api-practices)
8. [Comparison Matrix](#8-comparison-matrix)

---

## 1. Google API Design Guide

Google's API design guide is one of the most comprehensive and widely adopted standards.

### 1.1 Resource-Oriented Design

Google designs APIs around resources, not actions.

**Resource Hierarchy:**

```
APIs → Collections → Resources → Methods
```

**Naming Pattern:**

```
// Collection ID (plural noun)
publishers
books
shelves

// Resource names (full path)
//publishers/pub1/books/book2
//users/123/events/456

// Relative resource name
publishers/pub1/books/book2

// Resource ID
book2
```

**Standard Resource Operations:**

```protobuf
service LibraryService {
  // Standard Methods
  rpc ListBooks(ListBooksRequest) returns (ListBooksResponse);
  rpc GetBook(GetBookRequest) returns (Book);
  rpc CreateBook(CreateBookRequest) returns (Book);
  rpc UpdateBook(UpdateBookRequest) returns (Book);
  rpc DeleteBook(DeleteBookRequest) returns (google.protobuf.Empty);

  // Custom Methods (use colon syntax)
  rpc MoveBook(MoveBookRequest) returns (Book) {
    option (google.api.http) = {
      post: "/v1/{name=publishers/*/books/*}:move"
    };
  }
}
```

### 1.2 Standard Methods Mapping

| Method | HTTP | URI Path | Request Body | Response Body |
| ------ | ---- | -------- | ------------ | ------------- |
| List | GET | /collection | N/A | Resource list |
| Get | GET | /collection/{id} | N/A | Resource |
| Create | POST | /collection | Resource | Resource |
| Update | PUT/PATCH | /collection/{id} | Resource | Resource |
| Delete | DELETE | /collection/{id} | N/A | Empty |

### 1.3 Custom Methods

When standard methods don't fit, use custom methods with POST:

```http
# Use colon to separate custom verb
POST /v1/publishers/123/books/456:translate
POST /v1/projects/p1/locations/l1/datasets/d1:import
POST /v1/users/123:undelete
POST /v1/messages/456:batchGet
```

**Custom Method Examples:**

```http
# Cancel operation
POST /v1/operations/op123:cancel

# Move resource
POST /v1/folders/folder1:move
{
  "destination_parent": "folders/folder2"
}

# Batch operations
POST /v1/publishers/pub1/books:batchCreate
{
  "requests": [
    {"book": {...}},
    {"book": {...}}
  ]
}
```

### 1.4 Field Naming

```json
{
  "display_name": "My Resource",
  "create_time": "2023-06-15T10:30:00Z",
  "update_time": "2023-06-20T14:22:00Z",
  "delete_time": null,
  "expire_time": "2024-06-15T10:30:00Z",
  "start_time": "2023-06-15T09:00:00Z",
  "end_time": "2023-06-15T17:00:00Z",
  "read_time": "2023-06-15T10:30:00Z",
  "time_zone": "America/Los_Angeles",
  "region_code": "US",
  "language_code": "en-US",
  "mime_type": "application/json",
  "ip_address": "192.168.1.1",
  "filter": "status=ACTIVE",
  "order_by": "create_time desc",
  "etag": "abc123"
}
```

### 1.5 Pagination (Google Style)

```http
GET /v1/publishers/pub1/books?page_size=25

Response:
{
  "books": [...],
  "next_page_token": "CAESEjI..."
}

# Next page
GET /v1/publishers/pub1/books?page_size=25&page_token=CAESEjI...
```

**Implementation:**

```python
@app.get("/v1/publishers/{publisher_id}/books")
async def list_books(
    publisher_id: str,
    page_size: int = 25,
    page_token: Optional[str] = None
) -> ListBooksResponse:
    # Decode cursor
    cursor = decode_page_token(page_token) if page_token else None

    # Fetch page_size + 1 to check for more
    books = await db.books.find(
        {"publisher_id": publisher_id},
        cursor=cursor,
        limit=page_size + 1
    )

    # Check if more pages exist
    has_more = len(books) > page_size
    if has_more:
        books = books[:page_size]

    return ListBooksResponse(
        books=books,
        next_page_token=encode_page_token(books[-1].id) if has_more else None
    )
```

### 1.6 Field Masks

```http
# Select specific fields
GET /v1/books/123?read_mask=title,author

# Partial update
PATCH /v1/books/123?update_mask=title,price
{
  "title": "New Title",
  "price": 29.99,
  "ignored_field": "not updated"
}
```

### 1.7 Long-Running Operations

```http
POST /v1/projects/p1/locations/us/datasets:import

Response:
{
  "name": "projects/p1/locations/us/operations/op123",
  "metadata": {
    "@type": "type.googleapis.com/ImportDatasetMetadata",
    "progress_percent": 0
  },
  "done": false
}

# Poll for completion
GET /v1/projects/p1/locations/us/operations/op123

# Cancel operation
POST /v1/projects/p1/locations/us/operations/op123:cancel
```

### 1.8 Error Format

```json
{
  "error": {
    "code": 400,
    "message": "Request contains an invalid argument.",
    "status": "INVALID_ARGUMENT",
    "details": [
      {
        "@type": "type.googleapis.com/google.rpc.BadRequest",
        "fieldViolations": [
          {
            "field": "email",
            "description": "Not a valid email address"
          }
        ]
      },
      {
        "@type": "type.googleapis.com/google.rpc.Help",
        "links": [
          {
            "description": "API Documentation",
            "url": "https://cloud.google.com/docs"
          }
        ]
      }
    ]
  }
}
```

**Standard Error Codes:**

| Code | Status | Description |
| ---- | ------ | ----------- |
| 200 | OK | Success |
| 400 | INVALID_ARGUMENT | Invalid request |
| 401 | UNAUTHENTICATED | Missing/invalid auth |
| 403 | PERMISSION_DENIED | Not authorized |
| 404 | NOT_FOUND | Resource not found |
| 409 | ALREADY_EXISTS | Conflict |
| 429 | RESOURCE_EXHAUSTED | Rate limited |
| 500 | INTERNAL | Server error |
| 503 | UNAVAILABLE | Service unavailable |

---

## 2. Microsoft REST API Guidelines

Microsoft's guidelines emphasize consistency across all Microsoft APIs.

### 2.1 URL Structure

```
https://{service}.microsoft.com/api/{version}/{collection}/{id}
```

**Examples:**

```
https://graph.microsoft.com/v1.0/users
https://graph.microsoft.com/v1.0/users/123
https://graph.microsoft.com/v1.0/users/123/messages
https://management.azure.com/subscriptions/123/resourceGroups/rg1
```

### 2.2 HTTP Methods

| Method | Description | Idempotent |
| ------ | ----------- | ---------- |
| GET | Read resource | Yes |
| POST | Create resource or action | No |
| PUT | Create or replace resource | Yes |
| PATCH | Partial update | No |
| DELETE | Remove resource | Yes |

### 2.3 Response Format (OData)

```json
{
  "@odata.context": "https://graph.microsoft.com/v1.0/$metadata#users",
  "@odata.count": 50,
  "@odata.nextLink": "https://graph.microsoft.com/v1.0/users?$skiptoken=abc",
  "value": [
    {
      "@odata.type": "#microsoft.graph.user",
      "id": "user-id-123",
      "displayName": "John Doe",
      "mail": "john@example.com",
      "createdDateTime": "2023-06-15T10:30:00Z"
    }
  ]
}
```

### 2.4 Query Options

```http
# Select specific fields
GET /users?$select=id,displayName,mail

# Filter results
GET /users?$filter=startswith(displayName,'John')

# Order results
GET /users?$orderby=displayName desc

# Pagination
GET /users?$top=25&$skip=50

# Expand related entities
GET /users/123?$expand=manager,directReports

# Count
GET /users?$count=true

# Search
GET /users?$search="displayName:John"

# Combined
GET /users?$filter=department eq 'Engineering'&$orderby=hireDate desc&$top=10&$select=id,displayName
```

### 2.5 Actions and Functions

```http
# Actions (modify state)
POST /users/123/microsoft.graph.sendMail
{
  "message": {...}
}

# Functions (read-only)
GET /users/123/microsoft.graph.getMemberGroups
```

### 2.6 Batch Requests

```http
POST /$batch
Content-Type: application/json

{
  "requests": [
    {
      "id": "1",
      "method": "GET",
      "url": "/users/123"
    },
    {
      "id": "2",
      "method": "GET",
      "url": "/users/456"
    },
    {
      "id": "3",
      "method": "POST",
      "url": "/users",
      "body": {"displayName": "New User"},
      "headers": {"Content-Type": "application/json"}
    }
  ]
}

Response:
{
  "responses": [
    {
      "id": "1",
      "status": 200,
      "body": {...}
    },
    {
      "id": "2",
      "status": 200,
      "body": {...}
    },
    {
      "id": "3",
      "status": 201,
      "body": {...}
    }
  ]
}
```

### 2.7 Error Response

```json
{
  "error": {
    "code": "InvalidRequest",
    "message": "The request is invalid.",
    "target": "body",
    "details": [
      {
        "code": "NullValue",
        "target": "displayName",
        "message": "displayName cannot be null"
      }
    ],
    "innererror": {
      "code": "ValidationFailure",
      "message": "One or more properties failed validation",
      "date": "2023-06-15T10:30:00",
      "request-id": "abc-123",
      "client-request-id": "xyz-789"
    }
  }
}
```

### 2.8 Versioning

```http
# URL-based versioning
GET https://graph.microsoft.com/v1.0/users
GET https://graph.microsoft.com/beta/users

# API version header (Azure)
GET /resourceGroups
api-version: 2023-01-01
```

---

## 3. Amazon Web Services

AWS uses multiple API styles depending on the service.

### 3.1 REST APIs (Newer Services)

```http
# API Gateway, Lambda, S3
GET /2015-03-31/functions/{FunctionName}/configuration
PUT /2015-03-31/functions/{FunctionName}/code
POST /2015-03-31/functions/{FunctionName}/invocations

# S3
GET /{bucket}/{key}
PUT /{bucket}/{key}
DELETE /{bucket}/{key}
```

### 3.2 Query APIs (Older Services)

```http
# EC2, SQS, SNS
POST / HTTP/1.1
Host: ec2.amazonaws.com
Content-Type: application/x-www-form-urlencoded

Action=DescribeInstances
&Version=2016-11-15
&InstanceId.1=i-1234567890abcdef0
&Filter.1.Name=instance-type
&Filter.1.Value.1=t2.micro
```

### 3.3 Request Signing (Signature V4)

```python
import hashlib
import hmac
from datetime import datetime

def sign_request(
    method: str,
    url: str,
    headers: dict,
    body: bytes,
    access_key: str,
    secret_key: str,
    region: str,
    service: str
) -> dict:
    # Create signing key
    t = datetime.utcnow()
    date_stamp = t.strftime('%Y%m%d')
    amz_date = t.strftime('%Y%m%dT%H%M%SZ')

    # Step 1: Create canonical request
    canonical_request = create_canonical_request(
        method, url, headers, body
    )

    # Step 2: Create string to sign
    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = (
        f"AWS4-HMAC-SHA256\n"
        f"{amz_date}\n"
        f"{credential_scope}\n"
        f"{hash_sha256(canonical_request)}"
    )

    # Step 3: Calculate signature
    signing_key = get_signing_key(
        secret_key, date_stamp, region, service
    )
    signature = hmac.new(
        signing_key,
        string_to_sign.encode(),
        hashlib.sha256
    ).hexdigest()

    # Step 4: Add authorization header
    headers['Authorization'] = (
        f"AWS4-HMAC-SHA256 "
        f"Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={get_signed_headers(headers)}, "
        f"Signature={signature}"
    )
    headers['X-Amz-Date'] = amz_date

    return headers
```

### 3.4 Pagination (AWS Style)

```http
# DynamoDB
POST / HTTP/1.1
Content-Type: application/x-amz-json-1.0
X-Amz-Target: DynamoDB_20120810.Query

{
  "TableName": "Orders",
  "Limit": 25,
  "ExclusiveStartKey": {
    "pk": {"S": "USER#123"},
    "sk": {"S": "ORDER#456"}
  }
}

Response:
{
  "Items": [...],
  "Count": 25,
  "ScannedCount": 25,
  "LastEvaluatedKey": {
    "pk": {"S": "USER#123"},
    "sk": {"S": "ORDER#789"}
  }
}
```

### 3.5 Error Format

```json
{
  "error": {
    "type": "ValidationException",
    "message": "1 validation error detected: Value 'x' at 'name' failed to satisfy constraint: Member must have length greater than or equal to 1"
  }
}
```

```xml
<!-- XML format (S3, older services) -->
<Error>
  <Code>NoSuchKey</Code>
  <Message>The specified key does not exist.</Message>
  <Key>my-object-key</Key>
  <RequestId>abc123</RequestId>
</Error>
```

### 3.6 Common Headers

```http
# Request
X-Amz-Date: 20230615T103000Z
X-Amz-Content-Sha256: abc123...
X-Amz-Security-Token: session-token
X-Amz-Target: DynamoDB_20120810.GetItem

# Response
x-amz-request-id: abc123
x-amz-id-2: def456
x-amz-cf-id: cloudfront-id
```

---

## 4. Stripe API Design

Stripe is widely regarded as having one of the best API designs.

### 4.1 URL Structure

```
https://api.stripe.com/v1/{resource}
https://api.stripe.com/v1/{resource}/{id}
https://api.stripe.com/v1/{resource}/{id}/{sub_resource}
```

### 4.2 Object Structure

Every object has consistent structure:

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
  "email": "john@example.com"
}
```

### 4.3 List Operations

```http
GET /v1/customers?limit=25&starting_after=cus_abc

Response:
{
  "object": "list",
  "url": "/v1/customers",
  "has_more": true,
  "data": [
    {"id": "cus_def", "object": "customer", ...},
    {"id": "cus_ghi", "object": "customer", ...}
  ]
}
```

### 4.4 Expansions

```bash
# Expand nested object
curl https://api.stripe.com/v1/charges/ch_123 \
  -d "expand[]=customer"

# Multiple expansions
curl https://api.stripe.com/v1/invoices/in_123 \
  -d "expand[]=customer" \
  -d "expand[]=subscription.default_payment_method"

Response:
{
  "id": "ch_123",
  "customer": {
    "id": "cus_456",
    "object": "customer",
    "name": "John Doe"
  }
}
```

### 4.5 Metadata

```bash
# Create with metadata
curl https://api.stripe.com/v1/customers \
  -d email=john@example.com \
  -d "metadata[order_id]=12345" \
  -d "metadata[internal_id]=abc"

# Update metadata
curl https://api.stripe.com/v1/customers/cus_123 \
  -d "metadata[order_id]=67890"

# Delete specific metadata
curl https://api.stripe.com/v1/customers/cus_123 \
  -d "metadata[order_id]="
```

### 4.6 Idempotency

```bash
curl https://api.stripe.com/v1/charges \
  -H "Idempotency-Key: unique-request-id-abc123" \
  -d amount=2000 \
  -d currency=usd \
  -d source=tok_visa

# Replay returns same response
# Header in response: Idempotency-Replayed: true
```

### 4.7 Versioning

```bash
# Version via header
curl https://api.stripe.com/v1/charges \
  -H "Stripe-Version: 2023-10-16"

# Account default version
# Set in dashboard, used if header not provided
```

### 4.8 Error Handling

```json
{
  "error": {
    "type": "card_error",
    "code": "card_declined",
    "decline_code": "insufficient_funds",
    "message": "Your card has insufficient funds.",
    "param": "source",
    "doc_url": "https://stripe.com/docs/error-codes/card-declined",
    "charge": "ch_123"
  }
}
```

**Error Types:**

| Type | Description |
| ---- | ----------- |
| api_error | API problem (retry) |
| card_error | Card declined |
| idempotency_error | Idempotency key conflict |
| invalid_request_error | Invalid parameters |
| rate_limit_error | Too many requests |
| authentication_error | Invalid API key |

### 4.9 Webhooks

```json
{
  "id": "evt_abc123",
  "object": "event",
  "api_version": "2023-10-16",
  "created": 1623456789,
  "type": "customer.subscription.created",
  "livemode": true,
  "pending_webhooks": 1,
  "request": {
    "id": "req_xyz789",
    "idempotency_key": "key123"
  },
  "data": {
    "object": {
      "id": "sub_123",
      "object": "subscription",
      "customer": "cus_456"
    },
    "previous_attributes": {}
  }
}
```

**Webhook Signature Verification:**

```python
import stripe

def webhook_handler(request):
    payload = request.body
    sig_header = request.headers['Stripe-Signature']
    endpoint_secret = 'whsec_...'

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, endpoint_secret
        )
    except ValueError:
        return Response(status=400)
    except stripe.error.SignatureVerificationError:
        return Response(status=400)

    if event.type == 'payment_intent.succeeded':
        payment_intent = event.data.object
        handle_successful_payment(payment_intent)

    return Response(status=200)
```

---

## 5. GitHub API Design

GitHub's REST API v3 and GraphQL API v4.

### 5.1 REST API Structure

```http
GET https://api.github.com/repos/{owner}/{repo}
GET https://api.github.com/repos/{owner}/{repo}/issues
GET https://api.github.com/repos/{owner}/{repo}/pulls/{pull_number}
```

### 5.2 Authentication

```bash
# Personal Access Token
curl -H "Authorization: Bearer ghp_xxx" \
  https://api.github.com/user

# OAuth App
curl -H "Authorization: token oauth_token" \
  https://api.github.com/user

# GitHub App (JWT)
curl -H "Authorization: Bearer jwt_token" \
  https://api.github.com/app
```

### 5.3 Pagination

```http
GET /repos/owner/repo/issues?per_page=30&page=2

Response Headers:
Link: <https://api.github.com/repos/o/r/issues?page=3>; rel="next",
      <https://api.github.com/repos/o/r/issues?page=10>; rel="last",
      <https://api.github.com/repos/o/r/issues?page=1>; rel="first"
```

### 5.4 Conditional Requests

```bash
# Using ETag
curl -H "If-None-Match: \"abc123\"" \
  https://api.github.com/repos/owner/repo

# Returns 304 Not Modified if unchanged

# Using Last-Modified
curl -H "If-Modified-Since: Thu, 15 Jun 2023 10:30:00 GMT" \
  https://api.github.com/repos/owner/repo
```

### 5.5 Rate Limiting

```http
Response Headers:
X-RateLimit-Limit: 5000
X-RateLimit-Remaining: 4999
X-RateLimit-Used: 1
X-RateLimit-Reset: 1623456789
X-RateLimit-Resource: core
```

### 5.6 GraphQL API

```graphql
query {
  repository(owner: "facebook", name: "react") {
    name
    description
    stargazerCount
    issues(first: 5, states: OPEN) {
      nodes {
        title
        author {
          login
        }
        createdAt
      }
      pageInfo {
        hasNextPage
        endCursor
      }
    }
  }
}
```

### 5.7 Preview Features

```bash
# Opt-in to preview features
curl -H "Accept: application/vnd.github.machine-man-preview+json" \
  https://api.github.com/repos/owner/repo
```

---

## 6. Slack API Design

Slack uses a hybrid of REST and WebSocket APIs.

### 6.1 Web API

```bash
# POST form data
curl -X POST https://slack.com/api/chat.postMessage \
  -H "Authorization: Bearer xoxb-xxx" \
  -H "Content-Type: application/json" \
  -d '{
    "channel": "C1234567890",
    "text": "Hello, world!"
  }'

Response:
{
  "ok": true,
  "channel": "C1234567890",
  "ts": "1623456789.000100",
  "message": {...}
}
```

### 6.2 Response Structure

```json
{
  "ok": true,
  "channel": "C1234567890",
  "ts": "1623456789.000100"
}

{
  "ok": false,
  "error": "channel_not_found",
  "warning": "missing_charset",
  "response_metadata": {
    "warnings": ["missing_charset"],
    "messages": ["[ERROR] channel not found"]
  }
}
```

### 6.3 Pagination (Cursor-based)

```http
GET /api/conversations.history?channel=C123&limit=100

Response:
{
  "ok": true,
  "messages": [...],
  "has_more": true,
  "response_metadata": {
    "next_cursor": "dXNlcjpVMEC..."
  }
}

# Next page
GET /api/conversations.history?channel=C123&limit=100&cursor=dXNlcjpVMEC...
```

### 6.4 Events API (Webhooks)

```json
{
  "token": "verification_token",
  "team_id": "T1234567890",
  "api_app_id": "A1234567890",
  "event": {
    "type": "message",
    "channel": "C1234567890",
    "user": "U1234567890",
    "text": "Hello",
    "ts": "1623456789.000100"
  },
  "type": "event_callback",
  "event_id": "Ev1234567890",
  "event_time": 1623456789
}
```

### 6.5 Rate Limiting

```http
Response Headers:
Retry-After: 30

Response Body:
{
  "ok": false,
  "error": "ratelimited"
}
```

**Tier-based limits:**

| Tier | Requests/min | Methods |
| ---- | ------------ | ------- |
| Tier 1 | 1+ | Special |
| Tier 2 | 20 | Most POSTs |
| Tier 3 | 50 | Most GETs |
| Tier 4 | 100 | Web API |

---

## 7. Netflix API Practices

Netflix's approach to microservices APIs.

### 7.1 GraphQL Federation

Netflix uses Apollo Federation for their GraphQL APIs:

```graphql
# Studio API - Federated Graph
type Movie @key(fields: "id") {
  id: ID!
  title: String!
  director: Person!
  cast: [Person!]!
}

type Person @key(fields: "id") {
  id: ID!
  name: String!
  movies: [Movie!]!
}

# Query
query GetMovieDetails($id: ID!) {
  movie(id: $id) {
    title
    director {
      name
    }
    cast {
      name
    }
    ratings {
      average
      count
    }
  }
}
```

### 7.2 Resilience Patterns

```java
// Hystrix (legacy) / Resilience4j
@HystrixCommand(
    fallbackMethod = "getDefaultRecommendations",
    commandProperties = {
        @HystrixProperty(name = "execution.isolation.thread.timeoutInMilliseconds", value = "1000"),
        @HystrixProperty(name = "circuitBreaker.requestVolumeThreshold", value = "10"),
        @HystrixProperty(name = "circuitBreaker.errorThresholdPercentage", value = "50"),
        @HystrixProperty(name = "circuitBreaker.sleepWindowInMilliseconds", value = "5000")
    }
)
public List<Movie> getRecommendations(String userId) {
    return recommendationService.getForUser(userId);
}

public List<Movie> getDefaultRecommendations(String userId) {
    return popularMoviesCache.getTop10();
}
```

### 7.3 Zuul API Gateway

```java
// Pre-filter for authentication
public class AuthFilter extends ZuulFilter {
    @Override
    public Object run() {
        RequestContext ctx = RequestContext.getCurrentContext();
        String token = ctx.getRequest().getHeader("Authorization");

        if (!isValidToken(token)) {
            ctx.setResponseStatusCode(401);
            ctx.setSendZuulResponse(false);
        }
        return null;
    }
}

// Route filter for dynamic routing
public class RoutingFilter extends ZuulFilter {
    @Override
    public Object run() {
        RequestContext ctx = RequestContext.getCurrentContext();

        // A/B testing routing
        if (shouldRouteToCanary(ctx)) {
            ctx.setRouteHost(new URL("http://service-canary"));
        }
        return null;
    }
}
```

### 7.4 Response Caching

```http
# Cache-Control for CDN
Cache-Control: public, max-age=3600

# ETag for conditional requests
ETag: "version-hash-abc123"

# Vary for content negotiation
Vary: Accept-Encoding, Accept-Language
```

---

## 8. Comparison Matrix

### Authentication Methods

| Company | Primary Auth | Additional |
| ------- | ------------ | ---------- |
| Google | OAuth 2.0, API Key | Service Account |
| Microsoft | OAuth 2.0 | App-only, Delegated |
| AWS | Signature V4 | IAM, STS |
| Stripe | API Key | OAuth (Connect) |
| GitHub | OAuth, PAT | GitHub App, JWT |
| Slack | OAuth 2.0 | Bot Token, User Token |

### Pagination Styles

| Company | Style | Parameters |
| ------- | ----- | ---------- |
| Google | Token-based | `page_token`, `page_size` |
| Microsoft | OData | `$top`, `$skip`, `$skiptoken` |
| AWS | Cursor-based | `ExclusiveStartKey`, `Limit` |
| Stripe | Cursor-based | `starting_after`, `limit` |
| GitHub | Link headers | `per_page`, `page` |
| Slack | Cursor-based | `cursor`, `limit` |

### Versioning Approaches

| Company | Strategy | Example |
| ------- | -------- | ------- |
| Google | URL path | `/v1/`, `/v2/` |
| Microsoft | URL + Header | `/v1.0/`, `api-version` |
| AWS | URL path + Action | `/2015-03-31/`, `Version=` |
| Stripe | Header | `Stripe-Version: 2023-10-16` |
| GitHub | URL + Accept | `/v3/`, Preview headers |
| Slack | URL path | `/api/` (single version) |

### Error Response Format

| Company | Format | Fields |
| ------- | ------ | ------ |
| Google | gRPC Status | `code`, `message`, `details[]` |
| Microsoft | OData | `code`, `message`, `innererror` |
| AWS | Service-specific | `type`, `message`, `Code` |
| Stripe | Stripe Error | `type`, `code`, `message`, `param` |
| GitHub | GitHub Error | `message`, `errors[]`, `documentation_url` |
| Slack | Slack Error | `ok`, `error`, `warning` |

### Key Design Principles Summary

| Principle | Leaders | Implementation |
| --------- | ------- | -------------- |
| Resource-oriented | Google, GitHub | Noun-based URLs |
| Consistency | Stripe, Microsoft | Same patterns everywhere |
| Expandability | Stripe, GitHub | Embed related resources |
| Idempotency | Stripe, AWS | Idempotency keys |
| Pagination | All | Token/cursor-based |
| Rate limiting | All | Headers with limits |
| Error handling | Stripe, Google | Actionable error details |
| Versioning | All | URL or header-based |

---

## Recommendations for Your API

Based on these standards, here are recommendations:

### Adopt from Each

**From Google:**
- Resource-oriented design
- Field masks for partial responses
- Long-running operations pattern

**From Stripe:**
- Consistent object structure with `id`, `object`, `created`
- Expandable fields
- Idempotency keys
- Metadata on all objects

**From Microsoft:**
- OData query parameters for filtering/sorting
- Batch request support

**From GitHub:**
- Conditional requests with ETags
- Link headers for pagination
- Preview features for beta APIs

**From Slack:**
- Simple `ok` boolean for quick success check
- Clear error codes

### Universal Best Practices

1. Use HTTPS everywhere
2. Version your API from day one
3. Use cursor-based pagination
4. Include request IDs in responses
5. Implement rate limiting with clear headers
6. Provide comprehensive error messages with documentation links
7. Support idempotency for write operations
8. Use ISO 8601 for timestamps
9. Use consistent naming conventions
10. Document everything with OpenAPI/AsyncAPI
