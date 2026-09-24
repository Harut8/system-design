---
## System Design Task: FastAPI Role-Based Access Control (RBAC)

### Problem Statement

Design and implement a **production-grade Role-Based Access Control (RBAC)** system for a FastAPI application. The system must provide fine-grained authorization, support multi-tenancy, and scale to handle thousands of permission checks per second with minimal latency impact.

You are expected to design this as if it were going into **production at enterprise scale**.

---

### Functional Requirements

Your design must support:

1. **Role Management**

   * Define roles with hierarchical permissions:
     * Super Admin
     * Admin
     * Manager
     * User
     * Guest/Viewer
   * Support custom role creation
   * Role inheritance (Manager inherits User permissions)

2. **Permission Types**

   * **Route-level permissions**: Control access to API endpoints
   * **Resource-level permissions**: CRUD operations on specific resources
   * **Field-level permissions**: Control access to specific data fields
   * **Data-scope permissions**: Filter data based on ownership/tenant

3. **Authorization Models**

   * Role-Based Access Control (RBAC)
   * Optional: Attribute-Based Access Control (ABAC) extension
   * Support for permission groups/bundles

4. **Multi-Tenancy Support**

   * Tenant isolation for SaaS applications
   * Tenant-specific role definitions
   * Cross-tenant admin capabilities

5. **APIs**

   * User-Role assignment API
   * Role-Permission management API
   * Permission check API
   * Audit log API

6. **Integration Points**

   * JWT token-based authentication
   * OAuth2 scopes compatibility
   * Middleware-based enforcement
   * Dependency injection pattern

---

### Non-Functional Requirements

Your system must meet the following constraints:

1. **Performance**

   * Permission check latency ≤ **5ms** (P99)
   * Support **10,000+ permission checks/second** per instance
   * Cache hit ratio ≥ **95%** for repeated checks

2. **Scalability**

   * Support **100,000+ users**
   * Support **1,000+ roles**
   * Support **10,000+ permissions**

3. **Availability**

   * ≥ **99.9% uptime**
   * Graceful degradation when cache unavailable
   * No single point of failure

4. **Security**

   * Principle of least privilege by default
   * Audit trail for all permission changes
   * Token invalidation on role changes
   * Protection against privilege escalation

5. **Maintainability**

   * Clear separation of concerns
   * Easy to add new permissions
   * Migration support for permission changes

---

### What You Should Deliver

Provide a **practical, production-oriented design** that includes:

1. **Requirements clarification & assumptions**

2. **Database schema design**
   * User-Role relationships
   * Role-Permission mappings
   * Tenant isolation strategy

3. **Architecture components**
   * Core RBAC service
   * Caching layer
   * Middleware/dependency design

4. **FastAPI implementation patterns**
   * Dependency injection for permission checks
   * Decorator-based route protection
   * Request context management

5. **Caching strategy**
   * Permission cache design
   * Cache invalidation patterns
   * Fallback behavior

6. **Multi-tenancy implementation**
   * Tenant context propagation
   * Cross-tenant queries

7. **Audit and compliance**
   * Permission change tracking
   * Access attempt logging

8. **Testing strategy**
   * Unit testing permissions
   * Integration testing authorization flows

9. **Code examples**
   * Complete working implementation
   * Reusable patterns and utilities

10. **Trade-offs**
    * Explain design decisions and alternatives

---

### Expectations

* Be **concrete** (include actual Python code, SQL schemas, Redis patterns)
* Provide **working code examples** that can be adapted
* Follow **FastAPI best practices** (dependency injection, async patterns)
* Use **type hints** and **Pydantic models**
* Consider **real-world edge cases** (token expiry, role conflicts, etc.)
* Prefer **simple, maintainable designs** over clever abstractions

---

### Evaluation Criteria

* Correctness of authorization logic
* Performance under load
* Code quality and maintainability
* Security considerations
* Practical applicability to real projects

---

### Interview Kit

**Read first:** [Solution](../solutions/fastapi-rbac-design.md) · [API design patterns](../solutions/api-design-patterns.md) · [Caching](../distributed-systems/08-caching-strategies-and-patterns.md) §3 invalidation

**Curveballs.** The interviewer changes one thing mid-design. The hint in italics is what a strong answer reaches for:

1. An admin removes a role, and the user keeps access for 10 more minutes. *(Permission-cache invalidation, token lifetime vs. revocation.)*
2. A request for `/tenants/B/invoices` succeeds with a tenant-A token. Where is the check missing? *(Tenant scoping in every query, not only in the route.)*
3. Customers want custom roles. What changes in your model?
4. Auditors ask who could access payroll data on a given date last year.

**Must answer (security, privacy, operations):**

- Default deny, tests for every route's permission, and object-level (IDOR) checks
- An audit log of permission changes and access to sensitive resources

**Phase it (MVP → Growth → Scale):** MVP: roles in Postgres with a FastAPI dependency per route. Growth: tenant-scoped roles, a permission cache with invalidation. Scale: a policy engine (OPA/Cedar/Zanzibar-style) for relationships and custom roles.

Score yourself with the [rubric](README.md#scoring-rubric).
