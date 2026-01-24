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
    title="Distributed Counter - 100K Tier",
    description="Cached monolith counter service for up to 100K likes/second",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(router)


@app.get("/")
async def root():
    return {
        "service": "distributed-counter",
        "tier": "100K",
        "description": "PostgreSQL + Redis cached counter for growth scale",
    }
