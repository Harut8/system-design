# FastAPI RBAC Implementation

Production-grade Role-Based Access Control (RBAC) system for FastAPI applications.

## Features

- Role-based access control with hierarchical inheritance
- Permission caching with Redis
- Multi-tenancy support
- JWT authentication
- Audit logging
- Dependency injection patterns for route protection

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Set up environment
cp .env.example .env

# Run migrations
psql -U postgres -d rbac_db -f migrations/001_initial_schema.sql
psql -U postgres -d rbac_db -f migrations/002_seed_data.sql

# Start the application
uvicorn app.main:app --reload
```

## Project Structure

```
app/
├── main.py              # FastAPI application
├── config.py            # Settings
├── database.py          # Database connection
├── rbac/
│   ├── models.py        # SQLAlchemy models
│   ├── schemas.py       # Pydantic schemas
│   ├── service.py       # RBAC business logic
│   ├── cache.py         # Redis caching
│   ├── dependencies.py  # FastAPI dependencies
│   ├── decorators.py    # Permission decorators
│   └── exceptions.py    # Custom exceptions
├── auth/
│   ├── jwt.py           # JWT utilities
│   └── password.py      # Password hashing
└── api/v1/
    ├── auth.py          # Authentication endpoints
    ├── users.py         # User management
    ├── roles.py         # Role management
    └── permissions.py   # Permission management
```

## Usage Examples

### Dependency Injection

```python
from app.rbac.dependencies import require_permission, get_current_user

@router.get("/users")
async def list_users(
    auth: AuthContext = Depends(require_permission("users:read"))
):
    # Only users with users:read permission can access
    pass
```

### Decorator-Based

```python
from app.rbac.decorators import check_permission

@router.get("/admin")
@check_permission("admin:access")
async def admin_panel(
    auth: AuthContext = Depends(get_current_user)
):
    pass
```

### Programmatic Checks

```python
@router.get("/resource/{id}")
async def get_resource(
    id: UUID,
    auth: AuthContext = Depends(get_current_user)
):
    if auth.user_id == resource.owner_id or auth.has_permission("resources:read"):
        return resource
    raise PermissionDenied()
```

## API Endpoints

| Method | Endpoint | Permission Required |
|--------|----------|---------------------|
| POST | /api/v1/auth/token | None |
| GET | /api/v1/users | users:read |
| POST | /api/v1/users | users:write |
| DELETE | /api/v1/users/{id} | users:delete |
| GET | /api/v1/roles | roles:read |
| POST | /api/v1/roles | roles:write |
| GET | /api/v1/permissions | roles:read |
| POST | /api/v1/permissions | superuser |

## License

MIT
