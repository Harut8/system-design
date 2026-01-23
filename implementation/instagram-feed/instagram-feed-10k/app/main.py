from fastapi import FastAPI
from contextlib import asynccontextmanager

from app.api.routes import router
from app.database import engine
from app.config import get_settings

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    # Startup
    yield
    # Shutdown
    await engine.dispose()


app = FastAPI(
    title="Instagram Feed - 10K Tier",
    description="""
    Simple monolith feed service for up to 10K users.

    ## Features
    - Pull-based feed generation (fan-out-on-read)
    - Post creation and management
    - Social graph (follow/unfollow)
    - Interactions (like, comment)

    ## Architecture
    - Single PostgreSQL database
    - No caching layer
    - Synchronous feed generation at read time
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
        "tier": "10K",
        "description": "Simple PostgreSQL-based feed for early startup scale",
        "features": [
            "Pull-based feed generation",
            "Post CRUD",
            "Follow/Unfollow",
            "Like/Comment",
        ],
    }
