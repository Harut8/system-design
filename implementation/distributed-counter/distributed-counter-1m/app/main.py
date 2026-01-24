from fastapi import FastAPI
from contextlib import asynccontextmanager

from app.api.routes import router
from app.database import engine, redis_client
from app.services.kafka_producer import close_producer
from app.config import get_settings

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await close_producer()
    await engine.dispose()
    await redis_client.close()


app = FastAPI(
    title="Distributed Counter - 1M Tier",
    description="Async counter service with Kafka for up to 1M likes/second",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(router)


@app.get("/")
async def root():
    return {
        "service": "distributed-counter",
        "tier": "1M",
        "description": "Async processing with Kafka for scale-up",
        "features": ["kafka-events", "batched-aggregation", "eventual-consistency"],
    }
