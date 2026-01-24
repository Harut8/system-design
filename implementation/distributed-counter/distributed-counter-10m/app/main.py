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
    title="Distributed Counter - 10M Tier",
    description="Sharded counter service with hot item detection for up to 10M likes/second",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(router)


@app.get("/")
async def root():
    return {
        "service": "distributed-counter",
        "tier": "10M",
        "description": "Sharded counters with hot item detection for internet scale",
        "features": ["auto-sharding", "hot-detection", "eventual-consistency"],
    }
