import json
from aiokafka import AIOKafkaProducer
from app.config import get_settings

settings = get_settings()


class KafkaProducerService:
    """Kafka producer for publishing post events."""

    def __init__(self):
        self.producer = None

    async def start(self):
        self.producer = AIOKafkaProducer(
            bootstrap_servers=settings.kafka_bootstrap_servers,
            value_serializer=lambda v: json.dumps(v, default=str).encode("utf-8"),
        )
        await self.producer.start()

    async def stop(self):
        if self.producer:
            await self.producer.stop()

    async def publish_post_created(self, post_id: int, author_id: int, follower_count: int, is_celebrity: bool):
        """Publish post.created event."""
        if not self.producer:
            return

        event = {
            "event": "post.created",
            "post_id": post_id,
            "author_id": author_id,
            "follower_count": follower_count,
            "is_celebrity": is_celebrity,
        }
        await self.producer.send_and_wait("posts.created", event)

    async def publish_post_deleted(self, post_id: int, author_id: int):
        """Publish post.deleted event."""
        if not self.producer:
            return

        event = {
            "event": "post.deleted",
            "post_id": post_id,
            "author_id": author_id,
        }
        await self.producer.send_and_wait("posts.deleted", event)


kafka_producer = KafkaProducerService()
