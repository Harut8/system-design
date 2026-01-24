# FastAPI RBAC: Production-Grade Design & Implementation

> **Implementation Code**: See `implementation/fastapi-rbac/` for complete working code.

## 1. Requirements Clarification

### Questions & Answers

| Category | Question | Answer |
|----------|----------|--------|
| **Scale** | Number of users? | 100,000+ active users |
| **Scale** | Roles and permissions? | 1,000+ roles, 10,000+ permissions |
| **Performance** | Permission check latency? | ≤ 5ms P99 |
| **Performance** | Checks per second? | 10,000+ per instance |
| **Multi-tenancy** | Tenant isolation required? | Yes, full tenant isolation |
| **Auth** | Authentication method? | JWT with OAuth2 |
| **Storage** | Permission storage? | PostgreSQL + Redis cache |
| **Audit** | Logging requirements? | Full audit trail required |

### Key Assumptions

1. **Authentication is handled separately** - RBAC layer receives validated JWT tokens
2. **Roles are hierarchical** - Higher roles inherit lower role permissions
3. **Permissions are additive** - No explicit deny rules (simplifies logic)
4. **Cache-first architecture** - Permissions cached aggressively for performance
5. **Eventual consistency acceptable** - Permission changes propagate within seconds

---

## 2. High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              Client Applications                             │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                            API Gateway / Load Balancer                       │
│                         (Rate limiting, TLS termination)                     │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                              FastAPI Application                             │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────────────────┐  │
│  │  Auth Middleware │──│  RBAC Middleware │──│  Route Handlers (Business)  │  │
│  │  (JWT Validation)│  │  (Perm Check)    │  │                             │  │
│  └─────────────────┘  └─────────────────┘  └─────────────────────────────┘  │
│            │                   │                                             │
│            ▼                   ▼                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │                    Permission Service (Core RBAC Engine)             │    │
│  │   ┌─────────────┐  ┌─────────────┐  ┌─────────────┐                 │    │
│  │   │ Permission  │  │    Role     │  │   Tenant    │                 │    │
│  │   │   Checker   │  │   Resolver  │  │   Context   │                 │    │
│  │   └─────────────┘  └─────────────┘  └─────────────┘                 │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────────────┘
          │                        │                        │
          ▼                        ▼                        ▼
┌─────────────────┐      ┌─────────────────┐      ┌─────────────────┐
│   Redis Cache   │      │   PostgreSQL    │      │   Audit Log     │
│  (Permissions)  │      │   (Source of    │      │   (Kafka/PG)    │
│                 │      │    Truth)       │      │                 │
└─────────────────┘      └─────────────────┘      └─────────────────┘
```

---

## 3. Database Schema Design

> **SQL Migrations**: See `implementation/fastapi-rbac/migrations/`

### Core Tables

| Table | Purpose |
|-------|---------|
| `tenants` | Multi-tenancy support with settings |
| `users` | User accounts scoped to tenants |
| `permissions` | Permission definitions (resource:action) |
| `roles` | Role definitions with priority/hierarchy |
| `role_hierarchy` | Parent-child role relationships |
| `role_permissions` | Role to permission mappings |
| `user_roles` | User to role assignments |
| `permission_audit_log` | Audit trail for changes |

### Permission Code Convention

```
{resource}:{action}

Examples:
- users:read     - View user data
- users:write    - Create/update users
- users:delete   - Delete users
- posts:read     - View posts
- admin:access   - Access admin panel
```

### Default Roles Hierarchy

```
super_admin (priority: 100)
    └── admin (priority: 80)
        └── manager (priority: 60)
            └── user (priority: 40)
                └── viewer (priority: 20)
```

---

## 4. Implementation Structure

> **Complete Code**: See `implementation/fastapi-rbac/app/`

```
app/
├── main.py              # FastAPI application entry
├── config.py            # Settings management
├── database.py          # Async database connection
├── rbac/
│   ├── models.py        # SQLAlchemy models
│   ├── schemas.py       # Pydantic schemas
│   ├── service.py       # Core RBAC logic
│   ├── cache.py         # Redis caching layer
│   ├── dependencies.py  # FastAPI dependencies
│   ├── decorators.py    # Permission decorators
│   ├── constants.py     # Permission constants
│   └── exceptions.py    # Custom exceptions
├── auth/
│   ├── jwt.py           # JWT token utilities
│   └── password.py      # Password hashing
└── api/v1/
    ├── auth.py          # Authentication endpoints
    ├── users.py         # User management
    ├── roles.py         # Role management
    └── permissions.py   # Permission management
```

---

## 5. Key Components

### 5.1 Permission Checking Patterns

| Pattern | Use Case | Example |
|---------|----------|---------|
| **Dependency Injection** | Route-level protection | `Depends(require_permission("users:read"))` |
| **Decorator** | Alternative syntax | `@check_permission("admin:access")` |
| **Programmatic** | Conditional logic | `auth.has_permission("posts:write")` |
| **Role-based** | Role requirement | `Depends(require_role("admin"))` |
| **Multi-permission** | Any/All checks | `Depends(require_any_permission([...]))` |

### 5.2 AuthContext Schema

The `AuthContext` object provides authorization state:

| Field | Type | Description |
|-------|------|-------------|
| `user_id` | UUID | Current user ID |
| `tenant_id` | UUID | Current tenant ID |
| `email` | str | User email |
| `is_superuser` | bool | Superuser flag |
| `roles` | Set[str] | User's role codes |
| `permissions` | Set[str] | Resolved permissions |

### 5.3 Built-in Methods

| Method | Returns | Description |
|--------|---------|-------------|
| `has_permission(perm)` | bool | Check single permission |
| `has_any_permission([...])` | bool | Check any of list |
| `has_all_permissions([...])` | bool | Check all of list |
| `has_role(role)` | bool | Check role membership |

---

## 6. Caching Strategy

### Cache Architecture

```
Request → L1 Cache (Request) → L2 Cache (Redis) → Database (PostgreSQL)
                ↓                    ↓                    ↓
              Hit?                 Hit?               Query
                ↓                    ↓                    ↓
              Return             Return             Cache + Return
```

### Cache Keys

| Key Pattern | TTL | Purpose |
|-------------|-----|---------|
| `rbac:user:{tenant}:{user}:permissions` | 5 min | User permissions |
| `rbac:role:{role_id}:permissions` | 10 min | Role permissions |
| `rbac:permission:{code}` | 1 hour | Permission existence |

### Invalidation Triggers

| Event | Action |
|-------|--------|
| User role change | Invalidate user cache |
| Role permission change | Invalidate all users with role |
| Permission definition change | Full cache invalidation |

---

## 7. Multi-Tenancy

### Tenant Resolution Order

1. JWT token `tenant_id` claim (preferred)
2. `X-Tenant-ID` header
3. Subdomain extraction

### Tenant Isolation

- All queries filtered by `tenant_id`
- Roles can be global (`tenant_id = NULL`) or tenant-specific
- Users scoped to single tenant

---

## 8. API Endpoints

| Method | Endpoint | Permission |
|--------|----------|------------|
| POST | `/api/v1/auth/token` | None |
| GET | `/api/v1/users` | `users:read` |
| GET | `/api/v1/users/me` | Authenticated |
| POST | `/api/v1/users` | `users:write` |
| DELETE | `/api/v1/users/{id}` | `users:delete` |
| POST | `/api/v1/users/{id}/roles/{role_id}` | `roles:write` |
| GET | `/api/v1/roles` | `roles:read` |
| POST | `/api/v1/roles` | `roles:write` |
| GET | `/api/v1/permissions` | `roles:read` |
| POST | `/api/v1/permissions` | Superuser |

---

## 9. Best Practices Summary

### Security

| Practice | Implementation |
|----------|----------------|
| Least Privilege | Default role has minimal permissions |
| Defense in Depth | Check at route, service, and data layers |
| Token Invalidation | Invalidate cache on role changes |
| Audit Trail | Log all permission changes |
| Input Validation | Validate permission code format |

### Performance

| Practice | Implementation |
|----------|----------------|
| Aggressive Caching | Redis with 5-minute TTL |
| Batch Permission Loads | Load all at auth time |
| Index Optimization | Indexes on key columns |
| Connection Pooling | SQLAlchemy async pool |

### Code Organization

| Practice | Implementation |
|----------|----------------|
| Separation of Concerns | Auth, RBAC, Business separated |
| Dependency Injection | FastAPI `Depends()` pattern |
| Type Safety | Full Pydantic models |
| Reusable Components | Permission checks as dependencies |

---

## 10. Trade-offs & Decisions

| Decision | Chosen | Alternative | Rationale |
|----------|--------|-------------|-----------|
| Permission Storage | PostgreSQL | MongoDB | Relational, ACID, joins |
| Cache | Redis | Local memory | Shared, persistent |
| Auth Model | RBAC | ABAC | Simpler, 90% coverage |
| Check Location | Dependency Injection | Middleware | Explicit, testable |
| Role Inheritance | Explicit parent-child | Priority-based | Clear hierarchy |
| Deny Rules | Not supported | Explicit deny | Additive = simpler |
| Token Storage | Stateless JWT | Sessions | Scalable |
| Cache Invalidation | Event-based | TTL-only | Immediate consistency |

---

## 11. Production Checklist

- [ ] Configure proper JWT secret key (256+ bits)
- [ ] Set up Redis cluster for high availability
- [ ] Configure database connection pooling
- [ ] Enable audit logging to persistent storage
- [ ] Set up monitoring for permission check latency
- [ ] Configure rate limiting on auth endpoints
- [ ] Implement token refresh mechanism
- [ ] Set up backup for permission data
- [ ] Document all permissions in API docs
- [ ] Create permission migration strategy
- [ ] Test cache failover behavior
- [ ] Load test permission check throughput

---

## 12. Quick Start

```bash
cd implementation/fastapi-rbac

# Install dependencies
pip install -r requirements.txt

# Set up environment
cp .env.example .env

# Run migrations
psql -U postgres -d rbac_db -f migrations/001_initial_schema.sql
psql -U postgres -d rbac_db -f migrations/002_seed_data.sql

# Start application
uvicorn app.main:app --reload
```

---

## References

- [FastAPI Security Documentation](https://fastapi.tiangolo.com/tutorial/security/)
- [Permit.io FastAPI RBAC Tutorial](https://www.permit.io/blog/fastapi-rbac-full-implementation-tutorial)
- [Auth0 FGA with FastAPI](https://auth0.com/blog/implementing-rbac-fastapi-auth0-fga/)
- [Logto RBAC Documentation](https://docs.logto.io/api-protection/python/fastapi)
- [OWASP Authorization Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html)
