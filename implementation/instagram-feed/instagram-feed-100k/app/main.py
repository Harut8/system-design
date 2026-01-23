from fastapi import FastAPI
from contextlib import asynccontextmanager

from app.api.routes import router
from app.database import engine, redis_client
from app.config import get_settings

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    # Startup
    yield
    # Shutdown
    await engine.dispose()
    await redis_client.close()


app = FastAPI(
    title="Instagram Feed - 100K Tier",
    description="""
    Cached monolith feed service for up to 100K users.

    ## Features
    - Redis-cached feed generation
    - Basic fan-out-on-write for small accounts
    - Profile and post caching
    - Cache invalidation on updates

    ## Architecture
    - PostgreSQL for persistent storage
    - Redis for caching layer
    - Basic fan-out for accounts with < 5000 followers

    ## Cache Strategy
    - Feed results: 60 second TTL
    - User profiles: 5 minute TTL
    - Posts: 1 hour TTL
    """,
    version="1.0.0",
    lifespan=lifespan,
)

# Include routes
app.include_router(router)


@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "service": "instagram-feed",
        "tier": "100K",
        "description": "PostgreSQL + Redis cached feed for growing startup scale",
        "features": [
            "Redis feed caching",
            "Basic fan-out-on-write",
            "Profile caching",
            "Post caching",
            "Cache invalidation",
        ],
    }
