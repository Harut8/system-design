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
    title="Distributed Counter - 10K Tier",
    description="Simple monolith counter service for up to 10K likes/second",
    version="1.0.0",
    lifespan=lifespan,
)

# Include routes
app.include_router(router)


@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "service": "distributed-counter",
        "tier": "10K",
        "description": "Simple PostgreSQL-based counter for startup scale",
    }
