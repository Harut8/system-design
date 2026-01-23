from fastapi import FastAPI
from contextlib import asynccontextmanager

from app.api.routes import router
from app.database import engine, redis_client
from app.config import get_settings

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await engine.dispose()
    await redis_client.close()


app = FastAPI(
    title="Instagram Feed - 1M Tier",
    description="""
    Scalable feed service for up to 1M users with async processing.

    ## Features
    - Hybrid fan-out strategy (push/pull)
    - Celery workers for async fan-out
    - Celebrity post handling
    - Background job processing

    ## Architecture
    - PostgreSQL for persistent storage
    - Redis for caching and message broker
    - Celery for async task processing
    - Hybrid fan-out based on follower count
    """,
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(router)


@app.get("/")
async def root():
    return {
        "service": "instagram-feed",
        "tier": "1M",
        "description": "Async processing with Celery workers",
        "features": [
            "Hybrid fan-out strategy",
            "Async fan-out via Celery",
            "Celebrity post handling",
            "Background job processing",
        ],
    }
