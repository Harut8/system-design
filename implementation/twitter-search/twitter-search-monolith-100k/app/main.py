"""
Twitter Search Monolith - FastAPI Application

A production-ready monolith implementation of Twitter-like search functionality
using PostgreSQL full-text search and Redis caching.

Features:
- Full-text search with AND logic and relevance ranking
- Trending topics with 1-hour sliding window
- Autocomplete suggestions from multiple sources
- Engagement tracking (likes, retweets, views)
- Background tasks for cache refresh

Run with:
    uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import __version__
from app.api import router
from app.config import get_settings
from app.database import close_redis, engine, init_db, init_redis
from app.models import SEARCH_VECTOR_TRIGGER_SQL
from app.schemas import HealthResponse
from app.tasks import setup_scheduler, shutdown_scheduler
from app.tasks.scheduler import run_initial_tasks

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager for startup and shutdown."""
    logger.info("Starting Twitter Search Monolith...")

    # Initialize database
    logger.info("Initializing database...")
    await init_db()

    # Create search vector trigger
    async with engine.begin() as conn:
        try:
            for statement in SEARCH_VECTOR_TRIGGER_SQL.split(";"):
                statement = statement.strip()
                if statement:
                    await conn.execute(statement)
            logger.info("Search vector trigger created")
        except Exception as e:
            logger.warning(f"Could not create trigger (may already exist): {e}")

    # Initialize Redis
    logger.info("Initializing Redis...")
    await init_redis()

    # Start background scheduler
    logger.info("Starting background scheduler...")
    setup_scheduler()

    # Run initial cache population in background
    asyncio.create_task(run_initial_tasks())

    logger.info("Application startup complete!")

    yield

    # Shutdown
    logger.info("Shutting down...")
    shutdown_scheduler()
    await close_redis()
    await engine.dispose()
    logger.info("Shutdown complete")


# Create FastAPI application
app = FastAPI(
    title="Twitter Search Monolith",
    description="""
    A production-ready monolith implementation of Twitter-like search functionality.

    ## Features

    - **Search**: Full-text search with AND logic, ranked by relevance, recency, and engagement
    - **Trending**: Real-time trending topics with 1-hour sliding window
    - **Autocomplete**: Fast prefix-based suggestions from trending terms and popular queries
    - **Engagement**: Track likes, retweets, and views with buffered writes

    ## Architecture

    - PostgreSQL with full-text search (tsvector + GIN index)
    - Redis for caching and real-time data
    - Background tasks for cache refresh
    """,
    version=__version__,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure appropriately for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Global exception handler
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Handle unexpected exceptions."""
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": "Internal server error",
            "detail": str(exc) if settings.debug else None,
            "code": "INTERNAL_ERROR",
        },
    )


# Health check endpoint
@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["Health"],
    summary="Health check",
)
async def health_check() -> HealthResponse:
    """
    Check application health.

    Returns status of database and Redis connections.
    """
    from app.database import get_redis

    db_status = "healthy"
    redis_status = "healthy"

    # Check database
    try:
        from app.database import async_session_maker
        from sqlalchemy import text

        async with async_session_maker() as session:
            await session.execute(text("SELECT 1"))
    except Exception as e:
        db_status = f"unhealthy: {e}"

    # Check Redis
    try:
        redis_client = get_redis()
        await redis_client.ping()
    except Exception as e:
        redis_status = f"unhealthy: {e}"

    return HealthResponse(
        status="healthy" if db_status == "healthy" and redis_status == "healthy" else "degraded",
        database=db_status,
        redis=redis_status,
        version=__version__,
    )


# Include API routes
app.include_router(router, tags=["API"])


# Root endpoint
@app.get("/", tags=["Root"])
async def root():
    """Root endpoint with API information."""
    return {
        "name": "Twitter Search Monolith",
        "version": __version__,
        "docs": "/docs",
        "health": "/health",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.debug,
        log_level="info",
    )
