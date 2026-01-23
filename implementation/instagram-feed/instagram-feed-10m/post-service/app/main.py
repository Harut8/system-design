from fastapi import FastAPI
from contextlib import asynccontextmanager

from app.api.routes import router
from app.database import engine, redis_client
from app.services.kafka_producer import kafka_producer


@asynccontextmanager
async def lifespan(app: FastAPI):
    await kafka_producer.start()
    yield
    await kafka_producer.stop()
    await engine.dispose()
    await redis_client.close()


app = FastAPI(
    title="Post Service - 10M Tier",
    description="Microservice for post CRUD and event publishing",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(router)


@app.get("/")
async def root():
    return {
        "service": "post-service",
        "tier": "10M",
        "responsibilities": [
            "Post CRUD",
            "Post storage",
            "Event publishing to Kafka",
        ],
    }
