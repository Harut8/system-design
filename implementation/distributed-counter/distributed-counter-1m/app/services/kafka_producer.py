from aiokafka import AIOKafkaProducer
import json
from typing import Optional
from app.config import get_settings
from app.schemas import LikeEvent

settings = get_settings()

_producer: Optional[AIOKafkaProducer] = None


async def get_producer() -> AIOKafkaProducer:
    global _producer
    if _producer is None:
        _producer = AIOKafkaProducer(
            bootstrap_servers=settings.kafka_bootstrap_servers,
            value_serializer=lambda v: json.dumps(v).encode("utf-8"),
            key_serializer=lambda k: str(k).encode("utf-8"),
            acks=1,  # Leader ack only for speed
            enable_idempotence=False,  # Faster
        )
        await _producer.start()
    return _producer


async def produce_like_event(event: LikeEvent) -> None:
    """Produce like event to Kafka."""
    producer = await get_producer()

    # Partition by item_id for ordering per item
    await producer.send(
        settings.kafka_topic,
        key=event.item_id,
        value={
            "event_id": event.event_id,
            "user_id": event.user_id,
            "item_id": event.item_id,
            "item_type": event.item_type,
            "action": event.action,
            "timestamp": event.timestamp.isoformat(),
        },
    )


async def close_producer() -> None:
    global _producer
    if _producer:
        await _producer.stop()
        _producer = None
