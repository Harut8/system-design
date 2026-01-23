from fastapi import FastAPI
from contextlib import asynccontextmanager

from app.api.routes import router, feed_service, redis_client


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await feed_service.close()
    await redis_client.close()


app = FastAPI(
    title="Feed Service - 10M Tier",
    description="Microservice for feed assembly and caching",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(router)


@app.get("/")
async def root():
    return {
        "service": "feed-service",
        "tier": "10M",
        "responsibilities": [
            "Feed assembly",
            "Feed caching",
            "Celebrity post merging",
        ],
    }
